from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from websockets.asyncio.client import connect

from mba import __version__
from mba.evidence import atomic_json
from mba.protocol import AdapterError, Event, SessionConfig, new_id


class DockerHost:
    def __init__(self, root: Path, image: str, seccomp: Path):
        self.root, self.image, self.seccomp = root.resolve(), image, seccomp.resolve()

    async def command(self, *args, env=None, check=True):
        process = await asyncio.create_subprocess_exec(
            "docker", *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        communication = asyncio.create_task(process.communicate())
        try:
            async with asyncio.timeout(30):
                stdout, stderr = await asyncio.shield(communication)
        except asyncio.CancelledError:
            # Let a submitted Docker operation settle before cleaning up its named container.
            # Killing only the client can leave daemon-side creation running after lease release.
            try:
                await asyncio.wait_for(asyncio.shield(communication), 30)
            except TimeoutError:
                if process.returncode is None:
                    process.kill()
                await communication
            raise
        except BaseException:
            if process.returncode is None:
                process.kill()
            await communication
            raise
        if check and process.returncode:
            raise AdapterError("docker_failed", stderr.decode().strip(), 503)
        return stdout.decode().strip()

    async def inspect(self, name):
        try:
            data = await self.command("inspect", name)
        except AdapterError as exc:
            if any(text in exc.message.lower() for text in ("no such object", "no such container")):
                return None
            raise
        return json.loads(data)[0] if data and data != "[]" else None

    async def launch(self, sid: str, camera: str | None, worker_token: str):
        if not self.seccomp.is_file():
            raise AdapterError("seccomp_missing", "supply the deployment seccomp profile", 503)
        image = await self.command("image", "inspect", "--format", "{{.Id}}", self.image)
        session_root = self.root / sid
        session_root.mkdir(mode=0o700, exist_ok=True)
        # The worker runs as the host user's numeric ID so mounted evidence remains readable.
        env = {**os.environ, "MBA_TOKEN": worker_token}
        devices = (
            ["--group-add", str(os.stat(camera).st_gid), "--device", f"{camera}:/dev/video10:rw"]
            if camera
            else []
        )
        await self.command(
            "run",
            "--detach",
            "--init",
            "--name",
            f"mba-{sid}",
            "--label",
            "mba.managed=true",
            "--label",
            f"mba.session={sid}",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            *devices,
            "--ipc=private",
            "--shm-size=1g",
            "--cpus=4",
            "--memory=8g",
            "--pids-limit=512",
            "--security-opt",
            f"seccomp={self.seccomp}",
            "--cap-drop=ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--publish",
            "127.0.0.1::8080",
            "--env",
            "MBA_TOKEN",
            "--env",
            f"MBA_SESSION_ID={sid}",
            "--env",
            "HOME=/tmp/mba-home",
            "--mount",
            f"type=bind,src={session_root},dst=/artifacts",
            image,
            env=env,
        )
        info = await self.inspect(f"mba-{sid}")
        if not info or not info["State"]["Running"]:
            raise AdapterError("worker_exited", "worker exited during startup", 503)
        port = info["NetworkSettings"]["Ports"]["8080/tcp"][0]["HostPort"]
        return {"url": f"http://127.0.0.1:{port}", "image_id": image}

    async def remove(self, sid):
        try:
            await self.command("rm", "--force", f"mba-{sid}")
        except AdapterError as exc:
            if not any(
                text in exc.message.lower() for text in ("no such object", "no such container")
            ):
                raise


class Controller:
    def __init__(self, root: Path, token: str, cameras: list[str], host: DockerHost):
        if not token:
            raise ValueError("MBA_TOKEN is required")
        if len(cameras) != len(set(cameras)):
            raise ValueError("camera pool contains duplicates")
        self.root, self.token, self.cameras, self.host = root.resolve(), token, cameras, host
        self.root.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.root / "registry.json"
        self.registry = (
            json.loads(self.registry_path.read_text()) if self.registry_path.exists() else {}
        )
        self.lock = asyncio.Lock()
        self.http = httpx.AsyncClient(timeout=330)
        self.monitor = None
        self.lease_file = None
        self.monitor_error = None
        self.launch_tasks: dict[str, asyncio.Task] = {}

    def worker_token(self, sid):
        return hmac.new(self.token.encode(), sid.encode(), hashlib.sha256).hexdigest()

    def headers(self, sid):
        return {"Authorization": f"Bearer {self.worker_token(sid)}"}

    def persist(self):
        atomic_json(self.registry_path, self.registry)

    def entry(self, sid):
        if sid not in self.registry:
            raise AdapterError("session_not_found", "unknown session", 404)
        return self.registry[sid]

    def artifact(self, sid, path):
        self.entry(sid)
        root = (self.root / sid).resolve()
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            raise AdapterError("artifact_not_found", "artifact does not exist", 404)
        return candidate

    def manifest(self, sid):
        path = self.root / sid / "manifest.json"
        if path.exists():
            return json.loads(path.read_text())
        return {"session_id": sid, "state": self.entry(sid)["state"], "artifacts": []}

    def events(self, sid, after=0):
        self.entry(sid)
        path = self.root / sid / "events.sqlite3"
        if not path.exists():
            return []
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as db:
            rows = db.execute(
                "SELECT body FROM events WHERE sequence > ? ORDER BY sequence LIMIT 256",
                (max(0, after),),
            ).fetchall()
        return [Event.model_validate_json(row[0]) for row in rows]

    async def start(self):
        import fcntl

        self.lease_file = (self.root / "controller.lock").open("a")
        try:
            fcntl.flock(self.lease_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another controller owns this artifact root") from None
        # Reconcile all persisted leases before admitting new sessions.
        for sid, entry in self.registry.items():
            if entry["state"] in {"closed", "failed"}:
                continue
            info = await self.host.inspect(f"mba-{sid}")
            if info and info["State"]["Running"]:
                port = info["NetworkSettings"]["Ports"]["8080/tcp"][0]["HostPort"]
                entry["url"] = f"http://127.0.0.1:{port}"
                if self.manifest(sid).get("state") == "ready":
                    entry["state"] = "running"
                else:
                    await self.finish(sid, "startup_interrupted_by_controller_restart")
            else:
                await self.finish(sid, "worker_missing_after_restart")
        self.persist()
        self.monitor = asyncio.create_task(self._monitor())

    async def create(self, config: SessionConfig):
        async with self.lock:
            occupied = {
                v["camera"]
                for v in self.registry.values()
                if v["state"] not in {"closed", "failed"}
            }
            camera = (
                next((c for c in self.cameras if c not in occupied), None)
                if config.camera_enabled
                else None
            )
            if config.camera_enabled and camera is None:
                raise AdapterError("capacity_exhausted", "all camera leases are occupied", 429)
            if shutil.disk_usage(self.root).free < config.min_free_disk_mb * 1024**2:
                raise AdapterError("storage_full", "insufficient artifact disk space", 507)
            sid = new_id()
            self.registry[sid] = {"camera": camera, "state": "allocating"}
            self.launch_tasks[sid] = asyncio.current_task()
            self.persist()
        try:
            launched = await self.host.launch(sid, camera, self.worker_token(sid))
            self.registry[sid].update(launched)
            self.persist()
            async with asyncio.timeout(20):
                while True:
                    try:
                        response = await self.http.get(launched["url"] + "/health", timeout=1)
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.1)
            response = await self.http.post(
                launched["url"] + "/v1/sessions",
                headers=self.headers(sid),
                json=config.request_body(),
            )
            if response.is_error:
                raise AdapterError("startup_failed", response.text, 503)
            self.registry[sid]["state"] = "running"
            self.persist()
            return response.json()
        except BaseException as exc:
            await self.finish(sid, f"startup_failed: {exc}")
            raise
        finally:
            self.launch_tasks.pop(sid, None)

    async def finish(self, sid, failure=None):
        async with self.lock:
            entry = self.entry(sid)
            if entry["state"] in {"closed", "failed"}:
                return self.manifest(sid)
            await self.host.remove(sid)
            manifest = self.manifest(sid)
            if failure or manifest.get("state") not in {"closed", "failed"}:
                manifest.update(
                    state="failed", error=failure or "worker_terminated", incomplete=True
                )
                root = self.root / sid
                root.mkdir(exist_ok=True)
                atomic_json(root / "manifest.json", manifest)
            entry["state"] = manifest["state"]
            self.persist()
            return manifest

    async def stop_session(self, sid):
        entry = self.entry(sid)
        launch = self.launch_tasks.get(sid)
        if launch and launch is not asyncio.current_task():
            launch.cancel()
            await asyncio.gather(launch, return_exceptions=True)
        if entry["state"] in {"closed", "failed"}:
            return self.manifest(sid)
        failure = None
        if entry.get("url"):
            try:
                response = await self.http.post(
                    f"{entry['url']}/v1/sessions/{sid}/stop", headers=self.headers(sid), timeout=20
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                failure = f"shutdown_failed: {exc}"
        return await self.finish(sid, failure)

    async def _monitor(self):
        while True:
            await asyncio.sleep(1)
            for sid, entry in list(self.registry.items()):
                if entry["state"] != "running":
                    continue
                try:
                    manifest = self.manifest(sid)
                    if manifest.get("state") in {"closed", "failed"}:
                        await self.finish(sid)
                        continue
                    info = await self.host.inspect(f"mba-{sid}")
                    if not info or not info["State"]["Running"]:
                        await self.finish(sid, "worker_exited")
                    self.monitor_error = None
                except Exception as exc:
                    # Docker availability is not evidence that a camera lease can be reused.
                    self.monitor_error = str(exc)

    async def close(self):
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        # Leave healthy containers running for restart/reconciliation.
        await self.http.aclose()
        if self.lease_file:
            self.lease_file.close()


def create_controller_app(controller: Controller):
    @asynccontextmanager
    async def lifespan(app):
        await controller.start()
        try:
            yield
        finally:
            await controller.close()

    app = FastAPI(title="Multimodal Browser Adapter", version=__version__, lifespan=lifespan)

    @app.exception_handler(AdapterError)
    async def adapter_error(request, exc):
        return JSONResponse({"error": exc.code, "message": exc.message}, status_code=exc.status)

    async def authorize(authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization, f"Bearer {controller.token}"):
            raise AdapterError("unauthorized", "invalid bearer token", 401)

    @app.get("/health", dependencies=[Depends(authorize)])
    async def health():
        return {
            "status": "degraded" if controller.monitor_error else "ok",
            "capacity": len(controller.cameras),
            "error": controller.monitor_error,
        }

    @app.post("/v1/sessions", dependencies=[Depends(authorize)])
    async def start(config: SessionConfig):
        return await controller.create(config)

    @app.get("/v1/sessions", dependencies=[Depends(authorize)])
    async def sessions():
        return [
            {"session_id": sid, "state": entry["state"]}
            for sid, entry in controller.registry.items()
        ]

    @app.get("/v1/sessions/{sid}", dependencies=[Depends(authorize)])
    async def inspect(sid: str):
        controller.entry(sid)
        return controller.manifest(sid)

    @app.post("/v1/sessions/{sid}/stop", dependencies=[Depends(authorize)])
    async def stop(sid: str):
        return await controller.stop_session(sid)

    @app.get("/v1/sessions/{sid}/events", dependencies=[Depends(authorize)])
    async def events(sid: str, after: int = 0):
        return controller.events(sid, after)

    @app.get("/v1/sessions/{sid}/artifacts/{path:path}", dependencies=[Depends(authorize)])
    async def artifact(sid: str, path: str):
        if (
            not path.startswith(("recordings/", "frames/", "logs/", "downloads/"))
            and path != "manifest.json"
        ):
            raise AdapterError("artifact_not_found", "artifact not public", 404)
        return FileResponse(controller.artifact(sid, path))

    @app.api_route(
        "/v1/sessions/{sid}/{rest:path}", methods=["GET", "POST"], dependencies=[Depends(authorize)]
    )
    async def proxy(sid: str, rest: str, request: Request):
        entry = controller.entry(sid)
        if entry["state"] in {"closed", "failed"}:
            if rest.startswith("actions/") and request.method == "GET":
                aid = rest.split("/")[1]
                status = controller.manifest(sid).get("actions", {}).get(aid)
                if status:
                    return JSONResponse(status)
            raise AdapterError("session_closed", "session no longer accepts operations")
        if not entry.get("url"):
            raise AdapterError("session_not_ready", "session is allocating")
        url = f"{entry['url']}/v1/sessions/{sid}/{rest}"
        headers = {
            **controller.headers(sid),
            "content-type": request.headers.get("content-type", "application/json"),
        }
        upstream = controller.http.build_request(
            request.method,
            url,
            headers=headers,
            content=request.stream(),
            params=request.query_params,
        )
        try:
            response = await controller.http.send(upstream, stream=True)
        except httpx.HTTPError as exc:
            raise AdapterError("worker_unavailable", str(exc), 503) from exc

        async def body():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()

        return StreamingResponse(
            body(),
            status_code=response.status_code,
            media_type=response.headers.get("content-type"),
        )

    @app.websocket("/v1/sessions/{sid}/native/ws")
    async def native_ws(ws: WebSocket, sid: str):
        if not hmac.compare_digest(
            ws.headers.get("authorization", ""), f"Bearer {controller.token}"
        ):
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            from mba.ws_proxy import proxy_websocket

            entry = controller.entry(sid)
            if entry["state"] != "running":
                raise AdapterError("session_not_ready", "Session is not running")
            endpoint = entry["url"].replace("http://", "ws://", 1) + f"/v1/sessions/{sid}/native/ws"
            await proxy_websocket(ws, endpoint, controller.headers(sid))
        except Exception:
            pass
        finally:
            try:
                await ws.close()
            except RuntimeError:
                pass

    @app.websocket("/v1/sessions/{sid}/events/ws")
    async def events_ws(ws: WebSocket, sid: str, after: int = 0):
        if not hmac.compare_digest(
            ws.headers.get("authorization", ""), f"Bearer {controller.token}"
        ):
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            controller.entry(sid)
            while True:
                batch = controller.events(sid, after)
                for event in batch:
                    await asyncio.wait_for(ws.send_text(event.model_dump_json()), 5)
                    after = event.sequence
                if not batch and controller.entry(sid)["state"] in {"closed", "failed"}:
                    await ws.close()
                    return
                if not batch:
                    await asyncio.sleep(0.1)
        except (WebSocketDisconnect, TimeoutError):
            pass
        except AdapterError as exc:
            await ws.send_json({"error": exc.code, "message": exc.message})
            await ws.close(code=1008)

    @app.websocket("/v1/sessions/{sid}/capture/{kind}/ws")
    async def capture_ws(ws: WebSocket, sid: str, kind: str, channels: int = 2, fps: int = 1):
        if not hmac.compare_digest(
            ws.headers.get("authorization", ""), f"Bearer {controller.token}"
        ):
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            entry = controller.entry(sid)
            if entry["state"] != "running":
                raise AdapterError("session_not_ready", "session not running")
            if kind not in {"audio", "video"} or channels not in {1, 2} or not 1 <= fps <= 30:
                raise AdapterError("invalid_capture", "Invalid capture settings", 422)
            url = (
                entry["url"].replace("http://", "ws://", 1)
                + f"/v1/sessions/{sid}/capture/{kind}/ws?channels={channels}&fps={fps}"
            )
            async with connect(
                url, additional_headers=controller.headers(sid), max_size=8 * 1024**2
            ) as upstream:
                async for message in upstream:
                    if isinstance(message, bytes):
                        await asyncio.wait_for(ws.send_bytes(message), 5)
                    else:
                        await asyncio.wait_for(ws.send_text(message), 5)
            await ws.close()
        except (WebSocketDisconnect, OSError, TimeoutError):
            pass
        except AdapterError as exc:
            await ws.send_json({"error": exc.code, "message": exc.message})
            await ws.close(code=1008)

    @app.websocket("/v1/sessions/{sid}/streams/{stream_id}/ws")
    async def streams_ws(ws: WebSocket, sid: str, stream_id: str):
        if not hmac.compare_digest(
            ws.headers.get("authorization", ""), f"Bearer {controller.token}"
        ):
            await ws.close(code=1008)
            return
        await ws.accept()
        try:
            entry = controller.entry(sid)
            if entry["state"] != "running":
                raise AdapterError("session_not_ready", "session not running")
            url = (
                entry["url"].replace("http://", "ws://", 1)
                + f"/v1/sessions/{sid}/streams/{stream_id}/ws"
            )
            async with connect(
                url, additional_headers=controller.headers(sid), max_size=8 * 1024**2
            ) as upstream:

                async def outbound():
                    while True:
                        msg = await ws.receive()
                        if msg["type"] == "websocket.disconnect":
                            return
                        await upstream.send(
                            msg.get("bytes") if msg.get("bytes") is not None else msg["text"]
                        )

                async def inbound():
                    async for msg in upstream:
                        if isinstance(msg, bytes):
                            await ws.send_bytes(msg)
                        else:
                            await ws.send_text(msg)

                tasks = [asyncio.create_task(outbound()), asyncio.create_task(inbound())]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            await ws.close()
        except (WebSocketDisconnect, OSError):
            pass
        except AdapterError as exc:
            await ws.send_json({"error": exc.code, "message": exc.message})
            await ws.close(code=1008)

    return app
