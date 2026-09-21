from __future__ import annotations

import asyncio
import hmac
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from mba import __version__
from mba.protocol import (
    Action,
    AdapterError,
    BrowserOperation,
    CalibrationCommand,
    RecordingOperation,
    SessionConfig,
    StreamConfig,
    new_id,
)
from mba.session import Session
from mba.streams import StreamRegistry


def create_worker_app(root: Path, token: str, runtime_factory=None, session_id: str | None = None):
    if not token:
        raise ValueError("MBA_TOKEN is required")
    sessions: dict[str, Session] = {}
    streams = StreamRegistry()
    start_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        yield
        for session in sessions.values():
            await session.stop()

    app = FastAPI(title="Multimodal Browser Adapter worker", version=__version__, lifespan=lifespan)
    app.state.sessions = sessions
    app.state.streams = streams

    @app.exception_handler(AdapterError)
    async def adapter_error(request, exc):
        return JSONResponse({"error": exc.code, "message": exc.message}, status_code=exc.status)

    async def authorize(authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization, f"Bearer {token}"):
            raise AdapterError("unauthorized", "invalid bearer token", 401)

    def get_session(sid):
        if sid not in sessions:
            raise AdapterError("session_not_found", "unknown session", 404)
        return sessions[sid]

    async def ws_auth(ws):
        if not hmac.compare_digest(ws.headers.get("authorization", ""), f"Bearer {token}"):
            await ws.close(code=1008)
            return False
        await ws.accept()
        return True

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/harness")
    async def harness():
        return FileResponse(Path(__file__).parent / "harness" / "index.html")

    @app.post("/v1/sessions", dependencies=[Depends(authorize)])
    async def start(config: SessionConfig):
        config.reject_remote_browser()
        async with start_lock:
            if sessions:
                raise AdapterError("worker_occupied", "worker accepts exactly one session")
            sid = session_id or new_id()
            if runtime_factory:
                runtime = runtime_factory(streams)
            else:
                from mba.runtime_v2 import NativeRuntime

                runtime = NativeRuntime(
                    streams, camera=os.environ.get("MBA_CAMERA", "/dev/video10")
                )
            # The remote controller replays metadata from the shared disk journal.
            config = config.model_copy(update={"persistent_events": True})
            session = Session(sid, config, root, runtime)
            sessions[sid] = session
            try:
                await session.start()
            except Exception as exc:
                raise AdapterError("startup_failed", str(exc), 503) from exc
            return {"session_id": sid, "state": session.state}

    @app.get("/v1/sessions/{sid}", dependencies=[Depends(authorize)])
    async def inspect(sid: str):
        return get_session(sid).evidence.manifest

    @app.get("/v1/sessions/{sid}/capabilities", dependencies=[Depends(authorize)])
    async def capabilities(sid: str):
        from mba.capabilities import session_capabilities

        return await session_capabilities(get_session(sid))

    @app.get("/v1/sessions/{sid}/native", dependencies=[Depends(authorize)])
    async def native_descriptor(sid: str):
        descriptor = await get_session(sid).runtime.browser.native_descriptor()
        return {k: v for k, v in descriptor.items() if k not in {"endpoint", "headers"}}

    @app.post("/v1/sessions/{sid}/native/observations", dependencies=[Depends(authorize)])
    async def native_observations(sid: str, request: Request):
        data = await request.body()
        if len(data) > 1024**2:
            raise AdapterError("observations_too_large", "Observation batch exceeds 1 MB", 413)
        try:
            rows = json.loads(data)["rows"]
            if not isinstance(rows, list) or len(rows) > 256:
                raise ValueError()
            return await get_session(sid).runtime.browser.native_observe(rows)
        except (KeyError, TypeError, ValueError) as exc:
            raise AdapterError("invalid_observations", "Invalid observation batch", 422) from exc

    @app.websocket("/v1/sessions/{sid}/native/ws")
    async def native_ws(ws: WebSocket, sid: str):
        if not await ws_auth(ws):
            return
        try:
            from mba.ws_proxy import proxy_websocket

            descriptor = await get_session(sid).runtime.browser.native_descriptor()
            await proxy_websocket(ws, descriptor["endpoint"], descriptor["headers"])
        except Exception:
            pass
        finally:
            try:
                await ws.close()
            except RuntimeError:
                pass

    @app.post("/v1/sessions/{sid}/media/stop", dependencies=[Depends(authorize)])
    async def stop_media(sid: str, request: Request):
        kind = (await request.json()).get("kind")
        if kind not in {"audio", "video"}:
            raise AdapterError("invalid_modality", "Expected audio or video", 422)
        session = get_session(sid)
        if owner := session.owners.get(kind):
            await session.cancel(owner)
        return {}

    @app.post("/v1/sessions/{sid}/stop", dependencies=[Depends(authorize)])
    async def stop(sid: str):
        streams.abort_all("session stopped")
        return await get_session(sid).stop()

    @app.post("/v1/sessions/{sid}/actions", dependencies=[Depends(authorize)])
    async def submit(sid: str, action: Action):
        return await get_session(sid).submit(action)

    @app.get("/v1/sessions/{sid}/actions/{aid}", dependencies=[Depends(authorize)])
    async def action_status(sid: str, aid: str):
        session = get_session(sid)
        if aid not in session.statuses:
            raise AdapterError("action_not_found", "unknown action", 404)
        return session.statuses[aid]

    @app.post("/v1/sessions/{sid}/actions/{aid}/cancel", dependencies=[Depends(authorize)])
    async def cancel(sid: str, aid: str):
        return await get_session(sid).cancel(aid)

    @app.post("/v1/sessions/{sid}/assets", dependencies=[Depends(authorize)])
    async def upload(sid: str, request: Request):
        session = get_session(sid)
        session.check_disk()
        asset_id = await session.evidence.upload(request.stream())
        return {"asset_id": asset_id}

    @app.get("/v1/sessions/{sid}/artifacts/{path:path}", dependencies=[Depends(authorize)])
    async def artifact(sid: str, path: str):
        if (
            not path.startswith(("recordings/", "frames/", "logs/", "downloads/"))
            and path != "manifest.json"
        ):
            raise AdapterError("artifact_not_found", "artifact not public", 404)
        return FileResponse(get_session(sid).evidence.resolve(path))

    @app.get("/v1/sessions/{sid}/events", dependencies=[Depends(authorize)])
    async def events_page(sid: str, after: int = 0):
        return get_session(sid).evidence.read(max(0, after))

    @app.websocket("/v1/sessions/{sid}/events/ws")
    async def events_ws(ws: WebSocket, sid: str, after: int = 0):
        if not await ws_auth(ws):
            return
        try:
            session = get_session(sid)
            async for event in session.evidence.events(max(0, after)):
                await asyncio.wait_for(ws.send_text(event.model_dump_json()), 5)
            await ws.close(code=1000)
        except (WebSocketDisconnect, TimeoutError):
            pass
        except AdapterError as exc:
            await ws.send_json({"error": exc.code, "message": exc.message})
            await ws.close(code=1008)

    @app.post("/v1/sessions/{sid}/streams", dependencies=[Depends(authorize)])
    async def create_stream(sid: str, config: StreamConfig):
        if get_session(sid).state != "ready":
            raise AdapterError("session_not_ready", "session cannot accept input")
        stream = streams.create(config)
        return {"stream_id": stream.id, "config": config}

    @app.get("/v1/sessions/{sid}/calibration", dependencies=[Depends(authorize)])
    async def calibration(sid: str):
        return await get_session(sid).runtime.browser.calibration()

    @app.post("/v1/sessions/{sid}/calibration", dependencies=[Depends(authorize)])
    async def calibration_command(sid: str, command: CalibrationCommand):
        return await get_session(sid).runtime.browser.calibration(command)

    @app.post("/v1/sessions/{sid}/checkpoint", dependencies=[Depends(authorize)])
    async def checkpoint(sid: str):
        return await get_session(sid).runtime.browser.checkpoint()

    @app.post("/v1/sessions/{sid}/browser", dependencies=[Depends(authorize)])
    async def browser_command(sid: str, command: BrowserOperation):
        return await get_session(sid).runtime.browser.command(**command.model_dump())

    @app.post("/v1/sessions/{sid}/recording", dependencies=[Depends(authorize)])
    async def recording(sid: str, command: RecordingOperation):
        await get_session(sid).runtime.record(command.enabled)
        return {"enabled": command.enabled}

    @app.websocket("/v1/sessions/{sid}/capture/{kind}/ws")
    async def capture_ws(ws: WebSocket, sid: str, kind: str, channels: int = 2, fps: int = 1):
        if not await ws_auth(ws):
            return
        try:
            if kind not in {"audio", "video"} or channels not in {1, 2} or not 1 <= fps <= 30:
                raise AdapterError("invalid_capture", "Invalid capture settings", 422)
            session = get_session(sid)
            async for packet in session.runtime.capture(kind, channels=channels, fps=fps):
                # New output endpoint uses Protobuf packets; existing input framing is unchanged.
                await asyncio.wait_for(ws.send_bytes(packet.SerializeToString()), 5)
            await ws.close()
        except (WebSocketDisconnect, TimeoutError):
            pass
        except Exception as exc:
            await ws.send_json(
                {"error": getattr(exc, "code", "capture_failed"), "message": str(exc)}
            )
            await ws.close(code=1011)

    @app.websocket("/v1/sessions/{sid}/streams/{stream_id}/ws")
    async def input_ws(ws: WebSocket, sid: str, stream_id: str):
        if not await ws_auth(ws):
            return
        stream = None
        owner = False
        try:
            get_session(sid)
            stream = streams.get(stream_id)
            if stream.connected:
                raise AdapterError("stream_connected", "only one producer may connect")
            stream.connected, owner = True, True
            await ws.send_json({"ready": True, "config": stream.config.model_dump()})
            while True:
                message = await asyncio.wait_for(ws.receive(), 30)
                if message["type"] == "websocket.disconnect":
                    raise WebSocketDisconnect()
                if message.get("bytes") is not None:
                    await asyncio.wait_for(stream.put(message["bytes"]), 30)
                    await ws.send_json(
                        {"accepted_sequence": stream.last_sequence, "dropped": stream.dropped}
                    )
                elif json.loads(message.get("text", "{}")) == {"end": True}:
                    await asyncio.wait_for(stream.end(), 30)
                    await ws.send_json({"ended": True})
                    await ws.close()
                    return
                else:
                    raise AdapterError(
                        "invalid_stream_message", "expected binary frame or end message", 422
                    )
        except (AdapterError, ValueError, TimeoutError) as exc:
            await ws.send_json({"error": getattr(exc, "code", "stream_error"), "message": str(exc)})
            await ws.close(code=1008)
        except WebSocketDisconnect:
            if stream and not stream.closed:
                await get_session(sid).evidence.emit("input.disconnected", {"stream_id": stream_id})
        finally:
            if stream and owner and not stream.closed:
                stream.abort("producer disconnected before EOS")

    return app
