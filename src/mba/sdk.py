from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedError

from mba.protocol import (
    TERMINAL_ACTIONS,
    Action,
    ActionState,
    ActionStatus,
    AdapterError,
    AudioTrack,
    Event,
    SessionConfig,
    StreamConfig,
    TextTrack,
    VideoTrack,
)
from mba.streams import HEADER


class Client:
    def __init__(self, url: str, token: str, *, transport=None):
        self.url = url.rstrip("/")
        self.token = token
        self.http = httpx.AsyncClient(
            base_url=self.url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=330,
            transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.http.aclose()

    async def request(self, method, path, **kwargs):
        response = await self.http.request(method, path, **kwargs)
        if response.is_error:
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise AdapterError(
                body.get("error", "http_error"),
                body.get("message", response.text),
                response.status_code,
            )
        return response

    async def start_session(self, config: SessionConfig | None = None):
        config = config or SessionConfig()
        response = await self.request("POST", "/v1/sessions", json=config.request_body())
        return RemoteSession(self, response.json()["session_id"])

    def session(self, session_id: str):
        return RemoteSession(self, session_id)

    def websocket(self, path):
        url = self.url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        return connect(
            url + path,
            additional_headers={"Authorization": f"Bearer {self.token}"},
            max_size=8 * 1024 * 1024,
        )


class ActionHandle:
    def __init__(self, session, action_id):
        self.session, self.id = session, action_id

    async def status(self):
        response = await self.session.client.request(
            "GET", f"{self.session.path}/actions/{self.id}"
        )
        return ActionStatus.model_validate(response.json())

    async def wait(self, timeout_s: float = 130):
        async with asyncio.timeout(timeout_s):
            while True:
                status = await self.status()
                if status.state in TERMINAL_ACTIONS:
                    if status.state == ActionState.FAILED:
                        raise AdapterError("action_failed", status.error or "action failed")
                    return status
                await asyncio.sleep(0.05)

    async def cancel(self):
        return await self.session.cancel(self.id)


class RemoteSession:
    def __init__(self, client: Client, session_id: str):
        self.client, self.id = client, session_id
        self.path = f"/v1/sessions/{session_id}"
        from mba.library import Browser, Text, WebMCP

        self.browser, self.text, self.webmcp = Browser(self), Text(self), WebMCP(self)
        self.audio, self.camera = RemoteMedia(self, "audio"), RemoteMedia(self, "video")
        self.visual, self.recording = RemoteVisual(self), RemoteRecording(self)
        self._temporary = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        shutdown = asyncio.create_task(self.stop())
        try:
            await asyncio.shield(shutdown)
        except asyncio.CancelledError:
            await shutdown
            raise
        finally:
            if self._temporary:
                self._temporary.cleanup()

    async def capabilities(self):
        return (await self.client.request("GET", self.path + "/capabilities")).json()

    async def native_descriptor(self):
        result = (await self.client.request("GET", self.path + "/native")).json()
        result["endpoint"] = (
            self.client.url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
            + self.path
            + "/native/ws"
        )
        result["headers"] = {"Authorization": f"Bearer {self.client.token}"}
        return result

    async def native_observe(self, rows):
        return (
            await self.client.request(
                "POST", self.path + "/native/observations", json={"rows": rows}
            )
        ).json()

    def connect_native(self):
        from mba.native import NativeConnection

        return NativeConnection(self)

    async def stop_media(self, kind):
        return (
            await self.client.request("POST", self.path + "/media/stop", json={"kind": kind})
        ).json()

    async def screenshot(self):
        result = await self.checkpoint()
        if self._temporary is None:
            self._temporary = tempfile.TemporaryDirectory(prefix="mba-remote-")
        destination = Path(self._temporary.name) / Path(result["path"]).name
        await self.download(result["path"], destination)
        return destination

    async def inspect(self):
        return (await self.client.request("GET", self.path)).json()

    async def submit(self, action: Action):
        response = await self.client.request(
            "POST", self.path + "/actions", json=action.model_dump(mode="json")
        )
        return ActionHandle(self, response.json()["action_id"])

    async def upload(self, path: str | Path):
        async def chunks():
            source = await asyncio.to_thread(Path(path).open, "rb")
            try:
                while chunk := await asyncio.to_thread(source.read, 1024 * 1024):
                    yield chunk
            finally:
                await asyncio.to_thread(source.close)

        return (await self.client.request("POST", self.path + "/assets", content=chunks())).json()[
            "asset_id"
        ]

    async def send_text(self, content: str, **options):
        return await self.submit(Action(tracks=[TextTrack(content=content)], **options))

    async def send_audio(self, path: str | Path, **options):
        asset = await self.upload(path)
        return await self.submit(
            Action(tracks=[AudioTrack(source={"kind": "asset", "asset_id": asset})], **options)
        )

    async def send_video(self, path: str | Path, **options):
        asset = await self.upload(path)
        return await self.submit(
            Action(tracks=[VideoTrack(source={"kind": "asset", "asset_id": asset})], **options)
        )

    async def cancel(self, action_id: str):
        return (await self.client.request("POST", f"{self.path}/actions/{action_id}/cancel")).json()

    async def create_stream(self, config: StreamConfig):
        body = (
            await self.client.request("POST", self.path + "/streams", json=config.model_dump())
        ).json()
        return RemoteInputStream(self, body["stream_id"], config)

    async def events(self, after_sequence: int = 0):
        after = after_sequence
        while True:
            try:
                async with self.client.websocket(f"{self.path}/events/ws?after={after}") as ws:
                    async for message in ws:
                        data = json.loads(message)
                        if "error" in data:
                            raise AdapterError(
                                data["error"], data.get("message", "event subscription failed")
                            )
                        event = Event.model_validate(data)
                        if event.sequence > after:
                            after = event.sequence
                            yield event
                return
            except (OSError, ConnectionClosedError):
                await asyncio.sleep(0.5)

    async def event_page(self, after: int = 0):
        response = await self.client.request("GET", self.path + "/events", params={"after": after})
        return [Event.model_validate(item) for item in response.json()]

    async def download(self, artifact_path: str, destination: Path):
        async with self.client.http.stream(
            "GET", f"{self.path}/artifacts/{artifact_path}"
        ) as response:
            response.raise_for_status()
            with destination.open("wb") as output:
                async for chunk in response.aiter_bytes():
                    output.write(chunk)

    async def stop(self):
        return (await self.client.request("POST", self.path + "/stop")).json()

    async def checkpoint(self):
        return (await self.client.request("POST", self.path + "/checkpoint")).json()

    async def browser_command(self, operation, selector="", value="", **options):
        return (
            await self.client.request(
                "POST",
                self.path + "/browser",
                json={
                    "operation": operation,
                    "selector": selector,
                    "value": value,
                    **options,
                },
            )
        ).json()

    async def _recording(self, enabled=True):
        return (
            await self.client.request("POST", self.path + "/recording", json={"enabled": enabled})
        ).json()

    async def capture(self, kind="audio", *, channels=2, fps=1):
        from mba.generated.adapter_pb2 import MediaPacket
        from mba.library import decode_packet

        async with self.client.websocket(
            f"{self.path}/capture/{kind}/ws?channels={channels}&fps={fps}"
        ) as ws:
            async for message in ws:
                if not isinstance(message, bytes):
                    error = json.loads(message)
                    raise AdapterError(
                        error.get("error", "capture_failed"), error.get("message", "capture failed")
                    )
                yield decode_packet(MediaPacket.FromString(message))


class RemoteInputStream:
    def __init__(self, session, stream_id, config):
        self.session, self.id, self.config = session, stream_id, config

    @property
    def source(self):
        return {"kind": "stream", "stream_id": self.id}

    async def feed(self, packets):
        async with self.session.client.websocket(f"{self.session.path}/streams/{self.id}/ws") as ws:
            ready = json.loads(await ws.recv())
            if not ready.get("ready"):
                raise AdapterError("stream_failed", str(ready))
            index = 0
            origin = asyncio.get_running_loop().time()
            async for packet in packets:
                pts = (
                    index * 20_000
                    if self.config.kind == "audio"
                    else index * 1_000_000 // self.config.fps
                )
                await asyncio.sleep(max(0, origin + pts / 1e6 - asyncio.get_running_loop().time()))
                await ws.send(HEADER.pack(index, pts) + packet)
                acknowledgement = json.loads(await ws.recv())
                if "error" in acknowledgement:
                    raise AdapterError(acknowledgement["error"], acknowledgement["message"])
                index += 1
            await ws.send(json.dumps({"end": True}))
            acknowledgement = json.loads(await ws.recv())
            if not acknowledgement.get("ended"):
                raise AdapterError("stream_failed", str(acknowledgement))


class RemoteMedia:
    def __init__(self, session, kind):
        self.session, self.kind = session, kind

    async def play(self, path, **options):
        method = self.session.send_audio if self.kind == "audio" else self.session.send_video
        return await method(path, **options)

    def open_input(self, format=None):
        from mba.library import AudioFormat, VideoFormat

        format = format or (AudioFormat() if self.kind == "audio" else VideoFormat())
        if format.wire().kind != self.kind:
            raise ValueError(f"Expected a {self.kind} input format")
        return RemoteInput(self.session, format)

    async def stop(self):
        return await self.session.stop_media(self.kind)

    def capture(self, *, channels=2):
        if self.kind != "audio":
            raise AdapterError("unsupported_operation", "Use visual.frames()")
        return self.session.capture("audio", channels=channels)


class RemoteVisual:
    def __init__(self, session):
        self.session = session

    async def screenshot(self):
        return await self.session.screenshot()

    def frames(self, *, fps=1):
        if not 1 <= fps <= 30:
            raise ValueError("fps must be between 1 and 30")
        return self.session.capture("video", fps=fps)


class RemoteRecording:
    def __init__(self, session):
        self.session = session

    async def __call__(self, enabled=True):
        return await self.session._recording(enabled)

    async def start(self):
        return await self(True)

    async def stop(self):
        await self(False)
        return (await self.session.inspect())["artifacts"]


class RemoteInput:
    def __init__(self, session, format):
        f = format.wire()
        self.session, self.format = session, format
        self.config = StreamConfig(
            kind=f.kind,
            format=f.encoding,
            rate=f.rate,
            channels=f.channels,
            width=f.width,
            height=f.height,
            fps=f.fps,
        )
        self.index = 0
        self.origin = None
        self.action = self.ws = None
        self.closed = False

    async def __aenter__(self):
        try:
            stream = await self.session.create_stream(self.config)
            cls = AudioTrack if self.config.kind == "audio" else VideoTrack
            self.action = await self.session.submit(Action(tracks=[cls(source=stream.source)]))
            self.ws = await self.session.client.websocket(
                f"{self.session.path}/streams/{stream.id}/ws"
            )
            ready = json.loads(await self.ws.recv())
            if not ready.get("ready"):
                raise AdapterError("stream_failed", str(ready))
            return self
        except BaseException:
            await self.cancel()
            raise

    async def write(self, chunk):
        if self.closed:
            raise AdapterError("stream_closed", "Input stream is closed")
        data = chunk if isinstance(chunk, bytes) else chunk.data
        if len(data) != self.config.packet_bytes:
            raise ValueError(f"Expected {self.config.packet_bytes} bytes, received {len(data)}")
        if not isinstance(chunk, bytes) and chunk.format != self.format:
            raise ValueError("Chunk format differs from input format")
        pts = self.config.pts(self.index)
        now = asyncio.get_running_loop().time()
        if self.origin is None:
            self.origin = now
        await asyncio.sleep(max(0, self.origin + pts / 1e6 - now))
        await self.ws.send(HEADER.pack(self.index, pts) + data)
        ack = json.loads(await self.ws.recv())
        if ack.get("accepted_sequence") != self.index:
            raise AdapterError(ack.get("error", "stream_failed"), ack.get("message", str(ack)))
        self.index += 1

    async def cancel(self):
        self.closed = True
        if self.ws:
            await self.ws.close()
        if self.action:
            await self.action.cancel()

    async def __aexit__(self, exc_type, *_):
        try:
            if exc_type or self.closed:
                await self.cancel()
            else:
                await self.ws.send(json.dumps({"end": True}))
                ack = json.loads(await self.ws.recv())
                if not ack.get("ended"):
                    raise AdapterError("stream_failed", str(ack))
                await self.action.wait()
        except BaseException:
            await self.cancel()
            raise
        finally:
            self.closed = True
            if self.ws:
                await self.ws.close()
