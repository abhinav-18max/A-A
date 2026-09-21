"""Optional, provider-neutral bridges. Response policy belongs to the caller."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from mba.library import AudioChunk, VideoFrame
from mba.protocol import AdapterError, Event


@dataclass(frozen=True)
class Capabilities:
    text_input: bool = False
    audio_input: bool = False
    visual_input: bool = False
    text_output: bool = False
    audio_output: bool = False
    video_output: bool = False
    interruptions: bool = False


@dataclass(frozen=True)
class Output:
    response_id: str
    kind: str  # text, audio, video, end, or interrupt
    value: str | AudioChunk | VideoFrame | None = None


class Connector(Protocol):
    capabilities: Capabilities

    async def connect(self) -> None: ...
    async def send_text(self, observation: Event) -> None: ...
    async def send_audio(self, chunk: AudioChunk) -> None: ...
    async def send_visual(self, frame: VideoFrame) -> None: ...
    def outputs(self) -> AsyncIterator[Output]: ...
    async def interrupt(self, response_id: str) -> None: ...
    async def close(self) -> None: ...


@dataclass(frozen=True)
class Routes:
    target_text: bool = False
    target_audio: bool = False
    target_visual: bool = False
    output_text: bool = False
    output_audio: bool = False
    output_video: bool = False
    visual_fps: int = 1


class Bridge:
    """Explicit routes only: no automatic turn detection or interruption decisions."""

    def __init__(self, session, connector: Connector, routes: Routes):
        self.session, self.connector, self.routes = session, connector, routes
        self.tasks = []
        self.inputs = {}
        self.canceled = set()
        self.lock = asyncio.Lock()
        self.pending = {}

    async def __aenter__(self):
        c, r = self.connector.capabilities, self.routes
        if hasattr(self.session, "capabilities"):
            available = await self.session.capabilities()
            for requested, capability in (
                (r.target_audio, "audio_output"),
                (r.target_visual, "visual_output"),
                (r.output_text, "text_output"),
                (r.output_audio, "audio_input"),
                (r.output_video, "camera_input"),
            ):
                if requested and not available.get(capability):
                    raise AdapterError(
                        "session_capability", f"Session does not support {capability}", 422
                    )
        for requested, supported in (
            (r.target_text, c.text_input),
            (r.target_audio, c.audio_input),
            (r.target_visual, c.visual_input),
            (r.output_text, c.text_output),
            (r.output_audio, c.audio_output),
            (r.output_video, c.video_output),
        ):
            if requested and not supported:
                raise AdapterError("connector_capability", "Requested route is unsupported", 422)
        await self.connector.connect()
        if r.target_text:
            self.tasks.append(
                asyncio.create_task(
                    self._forward(self.session.text.observations(), self.connector.send_text)
                )
            )
        if r.target_audio:
            self.tasks.append(
                asyncio.create_task(
                    self._forward(self.session.audio.capture(channels=1), self.connector.send_audio)
                )
            )
        if r.target_visual:
            self.tasks.append(
                asyncio.create_task(
                    self._forward(
                        self.session.visual.frames(fps=r.visual_fps), self.connector.send_visual
                    )
                )
            )
        self.tasks.append(asyncio.create_task(self._output()))
        return self

    async def _forward(self, source, consumer):
        try:
            async for item in source:
                await consumer(item)
        finally:
            await source.aclose()

    async def _output(self):
        async for output in self.connector.outputs():
            if output.kind == "interrupt":
                await self.interrupt(output.response_id)
                continue
            if output.response_id in self.canceled:
                continue
            task = asyncio.create_task(self._deliver(output))
            self.pending[output.response_id] = task
            try:
                await task
            except asyncio.CancelledError:
                # A response interruption must not terminate the output consumer.
                # Cancellation of the bridge itself still propagates normally.
                if asyncio.current_task().cancelling() or output.response_id not in self.canceled:
                    raise
            finally:
                self.pending.pop(output.response_id, None)

    async def _deliver(self, output):
        if output.kind == "text":
            if not self.routes.output_text:
                raise AdapterError("route_disabled", "Text output route is disabled")
            action = await self.session.text.send(output.value)
            try:
                await action.wait()
            except asyncio.CancelledError:
                await action.cancel()
                raise
        elif output.kind in {"audio", "video"}:
            if not getattr(self.routes, "output_" + output.kind):
                raise AdapterError("route_disabled", "Media output route is disabled")
            key = (output.response_id, output.kind)
            if key not in self.inputs:
                media = self.session.audio if output.kind == "audio" else self.session.camera
                self.inputs[key] = await media.open_input(output.value.format).__aenter__()
            await self.inputs[key].write(output.value)
        elif output.kind == "end":
            for key in list(self.inputs):
                if key[0] == output.response_id:
                    # Retain the stream while EOS drains so interrupt can still cancel it.
                    await self.inputs[key].__aexit__(None, None, None)
                    self.inputs.pop(key, None)
        else:
            raise AdapterError("connector_output", f"Unknown output type: {output.kind}")

    async def interrupt(self, response_id):
        # Invalidate locally before waiting for a provider's cancellation acknowledgment.
        self.canceled.add(response_id)
        async with self.lock:
            if task := self.pending.get(response_id):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            for key in list(self.inputs):
                if key[0] == response_id:
                    stream = self.inputs.pop(key)
                    await stream.cancel()
                    await stream.__aexit__(None, None, None)
        if self.connector.capabilities.interruptions:
            await self.connector.interrupt(response_id)

    async def wait(self):
        done, _ = await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task

    async def __aexit__(self, *_):
        for task in self.tasks:
            task.cancel()
        results = await asyncio.gather(*self.tasks, return_exceptions=True)

        async def close_stream(stream):
            try:
                await stream.cancel()
            finally:
                await stream.__aexit__(None, None, None)

        results.extend(
            await asyncio.gather(
                *(close_stream(stream) for stream in self.inputs.values()), return_exceptions=True
            )
        )
        self.inputs.clear()
        results.extend(await asyncio.gather(self.connector.close(), return_exceptions=True))
        for result in results:
            if isinstance(result, Exception):
                raise result


class Subscription:
    """A bounded callback subscription; callback failure never runs in capture threads."""

    def __init__(self, events, callback, capacity=64, on_error=None):
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.events, self.callback = events, callback
        self.on_error = on_error
        self.queue = asyncio.Queue(capacity)
        self.task = None

    async def __aenter__(self):
        self.task = asyncio.create_task(self._run())
        return self

    async def _run(self):
        async def produce():
            async for event in self.events:
                try:
                    self.queue.put_nowait(event)
                except asyncio.QueueFull as exc:
                    raise AdapterError(
                        "subscription_overrun", "Callback subscription exceeded capacity"
                    ) from exc
            await self.queue.put(None)

        async def consume():
            while (event := await self.queue.get()) is not None:
                await self.callback(event)

        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(produce())
                group.create_task(consume())
        except Exception as exc:
            if self.on_error:
                await self.on_error(exc)
            raise

    async def wait(self):
        await self.task

    async def __aexit__(self, *_):
        self.task.cancel()
        result = (await asyncio.gather(self.task, return_exceptions=True))[0]
        if isinstance(result, Exception):
            raise result
