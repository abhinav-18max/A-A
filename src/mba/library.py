"""Public, in-process asynchronous API. Workers are managed implementation details."""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field

from mba.generated import adapter_pb2 as pb
from mba.protocol import (
    Action,
    ActionState,
    AdapterError,
    AssetSource,
    AudioTrack,
    BindingConfig,
    StreamSource,
    TextTrack,
    VideoTrack,
    new_id,
)
from mba.protocol import (
    SessionConfig as LegacyConfig,
)
from mba.runtime_v2 import NativeRuntime
from mba.session import Session


class SessionConfig(LegacyConfig):
    mode: Literal["text", "audio", "video", "multimodal"] = "text"
    binding: BindingConfig = Field(default_factory=lambda: BindingConfig(kind="manual"))
    recording: bool = True
    persistent_events: bool = False
    capture_tail_s: float = 0


@dataclass(frozen=True)
class AudioFormat:
    rate: int = 48000
    channels: int = 1
    encoding: str = "S16LE"

    def wire(self):
        if (
            self.rate not in (16000, 24000, 48000)
            or self.channels not in (1, 2)
            or self.encoding != "S16LE"
        ):
            raise ValueError("audio requires S16LE, 16/24/48 kHz, mono/stereo")
        return pb.MediaFormat(
            kind="audio", encoding=self.encoding, rate=self.rate, channels=self.channels
        )


@dataclass(frozen=True)
class VideoFormat:
    width: int = 1280
    height: int = 720
    fps: int = 30
    encoding: str = "RGB24"

    def wire(self):
        if (
            not (
                0 < self.width <= 1920
                and self.width % 2 == 0
                and 0 < self.height <= 1080
                and 0 < self.fps <= 60
            )
            or self.encoding not in ("RGB24", "YUY2")
            or (self.encoding == "RGB24" and self.width % 4)
        ):
            raise ValueError("unsupported video geometry/format")
        return pb.MediaFormat(
            kind="video", encoding=self.encoding, width=self.width, height=self.height, fps=self.fps
        )


@dataclass(frozen=True)
class AudioChunk:
    data: bytes
    sequence: int
    pts_us: int
    format: AudioFormat
    stream_id: str = "speaker"
    discontinuity: bool = False


@dataclass(frozen=True)
class VideoFrame:
    data: bytes
    sequence: int
    pts_us: int
    format: VideoFormat
    stream_id: str = "display"
    discontinuity: bool = False


def decode_packet(p):
    f = p.format
    if f.kind == "audio":
        return AudioChunk(
            p.data,
            p.sequence,
            p.pts_us,
            AudioFormat(f.rate, f.channels, f.encoding),
            p.stream_id,
            p.discontinuity,
        )
    return VideoFrame(
        p.data,
        p.sequence,
        p.pts_us,
        VideoFormat(f.width, f.height, f.fps, f.encoding),
        p.stream_id,
        p.discontinuity,
    )


class ActionHandle:
    def __init__(self, session, action_id):
        self.session, self.id = session, action_id

    async def status(self):
        return self.session.core.statuses[self.id].model_copy()

    async def wait(self, timeout_s=130):
        async with asyncio.timeout(timeout_s):
            await asyncio.shield(self.session.core.tasks[self.id])
        result = await self.status()
        if result.state == ActionState.FAILED:
            raise AdapterError("action_failed", result.error or "action failed")
        return result

    async def cancel(self):
        return await self.session.core.cancel(self.id)


class Browser:
    def __init__(self, session):
        self.session = session

    async def _command(self, operation, selector="", value="", **options):
        return await self.session.browser_command(operation, selector, value, **options)

    async def open(self, url):
        return await self._command("open", value=url)

    async def click(self, selector, **options):
        return await self._command("click", selector, **options)

    async def fill(self, selector, value, **options):
        return await self._command("fill", selector, value, **options)

    async def press(self, key, selector="", **options):
        return await self._command("press", selector, key, **options)

    async def wait_for(self, selector, **options):
        return await self._command("wait", selector, **options)

    async def command(self, operation, selector="", value="", **options):
        return await self._command(operation, selector, value, **options)

    async def snapshot(self, **options):
        return await self._command("snapshot", **options)

    async def pages(self):
        return await self._command("pages")

    async def new_page(self, url=""):
        return await self._command("new_page", value=url)

    async def select_page(self, page_id):
        return await self._command("select_page", page_id=page_id)

    async def close_page(self, page_id=""):
        return await self._command("close_page", page_id=page_id)

    async def double_click(self, selector="", **options):
        return await self._command("double_click", selector, **options)

    async def bind_text(self, binding, **options):
        data = binding.model_dump() if hasattr(binding, "model_dump") else binding
        return await self._command("bind_text", options={"binding": data}, **options)

    async def screenshot(self):
        return await self.session.screenshot()


class Text:
    def __init__(self, session):
        self.session = session

    async def bind(self, binding, **options):
        return await self.session.browser.bind_text(binding, **options)

    async def send(self, content, **options):
        return await self.session.submit(Action(tracks=[TextTrack(content=content)], **options))

    async def observations(self):
        async for event in self.session.events():
            if event.type.startswith("text."):
                yield event


class Input:
    def __init__(self, session, format):
        self.session, self.format = session, format.wire()
        self.id, self.ready = new_id(), asyncio.Event()
        self.index = 0
        self.closed = False
        self.origin = None
        self.call = self.action = None

    async def __aenter__(self):
        runtime = self.session.core.runtime
        if not runtime.media_stub:
            raise AdapterError("unsupported_modality", "Media is disabled", 422)
        runtime.native_streams[self.id] = self
        track_cls = AudioTrack if self.format.kind == "audio" else VideoTrack
        try:
            self.action = await self.session.submit(
                Action(tracks=[track_cls(source=StreamSource(stream_id=self.id))])
            )
            waiter = asyncio.create_task(self.ready.wait())
            task = self.session.core.tasks[self.action.id]
            try:
                done, _ = await asyncio.wait(
                    [waiter, task], timeout=20, return_when=asyncio.FIRST_COMPLETED
                )
                if waiter not in done:
                    if task.done():
                        await self.action.wait()
                    raise AdapterError("stream_not_ready", "Input stream preparation failed")
            finally:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
            self.call = runtime.media_stub.Input()
            return self
        except BaseException:
            if self.action:
                await self.action.cancel()
            runtime.native_streams.pop(self.id, None)
            raise

    async def write(self, chunk):
        if self.closed:
            raise AdapterError("stream_closed", "Input stream is closed")
        data = chunk if isinstance(chunk, bytes) else chunk.data
        f = self.format
        size = (
            f.rate // 50 * f.channels * 2
            if f.kind == "audio"
            else f.width * f.height * (3 if f.encoding == "RGB24" else 2)
        )
        if len(data) != size:
            raise ValueError(f"Expected {size} bytes, received {len(data)}")
        if not isinstance(chunk, bytes) and chunk.format.wire() != f:
            raise ValueError("Chunk format differs from input format")
        pts = self.index * 20_000 if f.kind == "audio" else self.index * 1_000_000 // f.fps
        now = asyncio.get_running_loop().time()
        if self.origin is None:
            self.origin = now
        await asyncio.sleep(max(0, self.origin + pts / 1e6 - now))
        await self.call.write(
            pb.MediaPacket(stream_id=self.id, sequence=self.index, pts_us=pts, data=data)
        )
        self.index += 1

    async def cancel(self):
        self.closed = True
        if self.call:
            self.call.cancel()
        if self.action:
            await self.action.cancel()

    async def __aexit__(self, exc_type, *_):
        try:
            if exc_type or self.closed:
                await self.cancel()
            else:
                pts = (
                    self.index * 20_000
                    if self.format.kind == "audio"
                    else self.index * 1_000_000 // self.format.fps
                )
                await self.call.write(
                    pb.MediaPacket(stream_id=self.id, sequence=self.index, pts_us=pts, end=True)
                )
                await self.call.done_writing()
                await self.call
                await self.action.wait()
        except BaseException:
            await self.cancel()
            raise
        finally:
            self.closed = True
            self.session.core.runtime.native_streams.pop(self.id, None)


class Media:
    def __init__(self, session, kind):
        self.session, self.kind = session, kind

    async def play(self, path, **options):
        asset = await self.session.upload(path)
        cls = AudioTrack if self.kind == "audio" else VideoTrack
        return await self.session.submit(
            Action(tracks=[cls(source=AssetSource(asset_id=asset))], **options)
        )

    def open_input(self, format=None):
        format = format or (AudioFormat() if self.kind == "audio" else VideoFormat())
        if format.wire().kind != self.kind:
            raise ValueError(f"Expected a {self.kind} input format")
        return Input(self.session, format)

    async def stop(self):
        await self.session.stop_media(self.kind)

    async def capture(self, *, channels=2):
        if self.kind != "audio":
            raise AdapterError(
                "unsupported_operation", "Use visual.frames() to capture browser output", 422
            )
        async for packet in self.session.core.runtime.capture("audio", channels=channels):
            yield decode_packet(packet)


class Visual:
    def __init__(self, session):
        self.session = session

    async def screenshot(self):
        return await self.session.browser.screenshot()

    async def frames(self, *, fps=1):
        if not 1 <= fps <= 30:
            raise ValueError("fps must be between 1 and 30")
        async for packet in self.session.core.runtime.capture("video", fps=fps):
            yield decode_packet(packet)


class Recording:
    def __init__(self, session):
        self.session = session

    async def start(self):
        await self.session.core.runtime.record(True)

    async def stop(self):
        await self.session.core.runtime.record(False)
        return self.session.core.evidence.manifest["artifacts"]


class Adapter:
    def __init__(
        self,
        config: LegacyConfig | None = None,
        *,
        artifact_dir: str | Path | None = None,
        camera: str | None = None,
    ):
        self.config = config or SessionConfig()
        self.artifact_dir = artifact_dir or (
            Path.cwd() / "artifacts" if self.config.recording else None
        )
        self.camera_device = camera
        self.core = None
        self.temporary = None
        self.browser, self.text = Browser(self), Text(self)
        self.audio, self.camera = Media(self, "audio"), Media(self, "video")
        self.visual, self.recording = Visual(self), Recording(self)

    async def __aenter__(self):
        if self.core:
            raise AdapterError("session_started", "Adapter instances are single-use")
        sid = new_id()
        if self.artifact_dir:
            root = Path(self.artifact_dir).resolve() / sid
        else:
            self.temporary = tempfile.TemporaryDirectory(prefix="mba-artifacts-")
            root = Path(self.temporary.name)
        config = self.config.model_copy(update={"persistent_events": bool(self.artifact_dir)})
        self.core = Session(sid, config, root, NativeRuntime(camera=self.camera_device))
        try:
            await self.core.start()
        except BaseException:
            self.core.evidence.db.close()
            if self.temporary:
                self.temporary.cleanup()
            raise
        return self

    @property
    def id(self):
        return self.core.id

    async def inspect(self):
        return dict(self.core.evidence.manifest)

    async def capabilities(self):
        from mba.capabilities import session_capabilities

        return await session_capabilities(self.core)

    async def browser_command(self, operation, selector="", value="", **options):
        return await self.core.runtime.browser.command(operation, selector, value, **options)

    async def screenshot(self):
        result = await self.core.runtime.browser.checkpoint()
        return self.core.evidence.resolve(result["path"])

    async def stop_media(self, kind):
        if owner := self.core.owners.get(kind):
            await self.core.cancel(owner)

    async def native_descriptor(self):
        return await self.core.runtime.browser.native_descriptor()

    async def native_observe(self, rows):
        return await self.core.runtime.browser.native_observe(rows)

    def connect_native(self):
        from mba.native import NativeConnection

        return NativeConnection(self)

    async def submit(self, action):
        result = await self.core.submit(action)
        return ActionHandle(self, result.action_id)

    async def upload(self, path):
        async def chunks():
            source = await asyncio.to_thread(Path(path).open, "rb")
            try:
                while data := await asyncio.to_thread(source.read, 1024 * 1024):
                    yield data
            finally:
                await asyncio.to_thread(source.close)

        return await self.core.evidence.upload(chunks())

    def events(self, after_sequence=0):
        return self.core.evidence.events(after_sequence)

    def subscribe(self, callback, *, capacity=64):
        from mba.connectors import Subscription

        async def failed(error):
            await self.core.evidence.emit("extension.failed", {"reason": str(error)})

        return Subscription(self.events(), callback, capacity=capacity, on_error=failed)

    def media_endpoint(self):
        """Private session-scoped UDS endpoint for native connector integrations."""
        runtime = self.core.runtime
        if not runtime.media:
            raise AdapterError("unsupported_modality", "Media is disabled", 422)
        return {
            "uri": f"unix:{runtime.media.socket}",
            "protocol_version": 1,
            "session_offset_us": runtime.offset,
        }

    async def stop(self):
        return await self.core.stop()

    async def __aexit__(self, *_):
        shutdown = asyncio.create_task(self.stop())
        try:
            try:
                await asyncio.shield(shutdown)
            except asyncio.CancelledError:
                await shutdown
                raise
        finally:
            self.core.evidence.db.close()
            if self.temporary:
                self.temporary.cleanup()
