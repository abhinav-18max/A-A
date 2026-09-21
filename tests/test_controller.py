import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from mba.controller import Controller, DockerHost
from mba.evidence import atomic_json
from mba.protocol import AdapterError, SessionConfig


@pytest.mark.parametrize(
    "message", ["No such object", "no such object", "No such container", "no such container"]
)
async def test_missing_container_messages_across_docker_versions(tmp_path, message):
    host = DockerHost(tmp_path, "test", tmp_path / "seccomp.json")
    host.command = AsyncMock(
        side_effect=AdapterError("docker_failed", f"error: {message}: mba-test")
    )
    assert await host.inspect("mba-test") is None
    await host.remove("test")


async def test_real_host_errors_are_not_treated_as_missing_containers(tmp_path):
    host = DockerHost(tmp_path, "test", tmp_path / "seccomp.json")
    host.command = AsyncMock(side_effect=AdapterError("docker_failed", "daemon unavailable"))
    with pytest.raises(AdapterError, match="daemon unavailable"):
        await host.inspect("mba-test")
    with pytest.raises(AdapterError, match="daemon unavailable"):
        await host.remove("test")


class FakeHost:
    def __init__(self, root):
        self.root = root
        self.running = {}
        self.launch_gate = None
        self.removed = []
        self.unavailable = False

    async def inspect(self, name):
        if self.unavailable:
            raise AdapterError("docker_failed", "daemon unavailable")
        return self.running.get(name)

    async def launch(self, sid, camera, token):
        if self.launch_gate:
            await self.launch_gate.wait()
        (self.root / sid).mkdir(exist_ok=True)
        self.running[f"mba-{sid}"] = {
            "State": {"Running": True},
            "NetworkSettings": {"Ports": {"8080/tcp": [{"HostPort": "12345"}]}},
        }
        return {"url": "http://worker", "image_id": "sha256:test"}

    async def remove(self, sid):
        if self.unavailable:
            raise AdapterError("docker_failed", "daemon unavailable")
        self.running.pop(f"mba-{sid}", None)
        self.removed.append(sid)


@pytest.fixture
async def controller(tmp_path):
    host = FakeHost(tmp_path)
    ctrl = Controller(tmp_path, "test-token", ["camera1", "camera2"], host)
    await ctrl.http.aclose()

    def transport(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/sessions":
            sid = next(sid for sid, e in ctrl.registry.items() if e["state"] == "allocating")
            atomic_json(
                tmp_path / sid / "manifest.json",
                {"session_id": sid, "state": "ready", "artifacts": []},
            )
            return httpx.Response(200, json={"session_id": sid, "state": "ready"})
        if request.url.path.endswith("/stop"):
            sid = request.url.path.split("/")[3]
            atomic_json(
                tmp_path / sid / "manifest.json",
                {"session_id": sid, "state": "closed", "artifacts": []},
            )
            return httpx.Response(200, json={"state": "closed"})
        return httpx.Response(404)

    ctrl.http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    yield ctrl
    await ctrl.close()


async def test_camera_leases_are_unique_and_third_session_rejected(controller):
    a = await controller.create(SessionConfig())
    b = await controller.create(SessionConfig())
    assert a["session_id"] != b["session_id"]
    assert len({e["camera"] for e in controller.registry.values()}) == 2
    with pytest.raises(AdapterError, match="occupied"):
        await controller.create(SessionConfig())
    await controller.stop_session(a["session_id"])
    c = await controller.create(SessionConfig())
    assert controller.registry[c["session_id"]]["camera"] == "camera1"


async def test_cancelled_launch_cleans_up_lease(controller):
    controller.host.launch_gate = asyncio.Event()
    task = asyncio.create_task(controller.create(SessionConfig()))
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert list(controller.registry.values())[0]["state"] == "failed"
    assert len(controller.host.removed) == 1


async def test_docker_outage_does_not_release_camera(controller):
    result = await controller.create(SessionConfig())
    controller.host.unavailable = True
    with pytest.raises(AdapterError):
        await controller.finish(result["session_id"], "outage")
    assert controller.entry(result["session_id"])["state"] == "running"


async def test_restart_reconciles_live_worker_without_replaying_actions(controller):
    result = await controller.create(SessionConfig())
    await controller.start()
    assert controller.entry(result["session_id"])["state"] == "running"
    assert not controller.host.removed


async def test_stop_allocating_session_cancels_start_before_releasing_lease(controller):
    controller.host.launch_gate = asyncio.Event()
    launch = asyncio.create_task(controller.create(SessionConfig()))
    await asyncio.sleep(0.01)
    sid = next(iter(controller.registry))
    result = await controller.stop_session(sid)
    assert result["state"] == "failed"
    assert launch.done()
    assert controller.host.removed == [sid]


async def test_restart_marks_missing_worker_incomplete(controller):
    result = await controller.create(SessionConfig())
    controller.host.running.clear()
    await controller.start()
    manifest = controller.manifest(result["session_id"])
    assert manifest["state"] == "failed"
    assert manifest["incomplete"] is True
    assert (
        json.loads(controller.registry_path.read_text())[result["session_id"]]["state"] == "failed"
    )


async def test_text_and_audio_do_not_reserve_cameras(controller):
    controller.cameras = []
    from mba import SessionConfig as LibraryConfig

    a = await controller.create(LibraryConfig())
    b = await controller.create(LibraryConfig(mode="audio"))
    assert controller.entry(a["session_id"])["camera"] is None
    assert controller.entry(b["session_id"])["camera"] is None
