import httpx
from fastapi.testclient import TestClient

from mba.protocol import SessionConfig
from mba.sdk import Client
from mba.service import create_worker_app
from mba.streams import HEADER

TOKEN = "test-only-token-with-enough-characters"


async def test_sdk_against_worker_api(tmp_path, runtime_factory):
    app = create_worker_app(tmp_path, TOKEN, runtime_factory)
    async with Client("http://test", TOKEN, transport=httpx.ASGITransport(app=app)) as client:
        session = await client.start_session(SessionConfig(capture_tail_s=0, min_free_disk_mb=1))
        action = await session.send_text("hello")
        assert (await action.wait()).state == "completed"
        fixture = tmp_path / "fixture.wav"
        fixture.write_bytes(b"fake test fixture")
        asset = await session.upload(fixture)
        assert (tmp_path / "assets" / asset).read_bytes() == fixture.read_bytes()
        events = await session.event_page()
        assert any(e.type == "action.status" for e in events)
        result = await session.stop()
        assert result["state"] == "closed"


async def test_unauthorized_request_cannot_start_worker(tmp_path, runtime_factory):
    app = create_worker_app(tmp_path, TOKEN, runtime_factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/v1/sessions", json={})
        assert response.status_code == 401
        assert not app.state.sessions


def test_event_socket_replays_and_closes_on_stop(tmp_path, runtime_factory):
    app = create_worker_app(tmp_path, TOKEN, runtime_factory)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(app, headers=headers) as client:
        sid = client.post("/v1/sessions", json={"capture_tail_s": 0, "min_free_disk_mb": 1}).json()[
            "session_id"
        ]
        with client.websocket_connect(f"/v1/sessions/{sid}/events/ws", headers=headers) as ws:
            assert ws.receive_json()["type"] == "session.state"
            client.post(f"/v1/sessions/{sid}/stop")
            assert ws.receive_json()["data"]["state"] == "draining"
            assert ws.receive_json()["data"]["state"] == "closed"


def test_stream_socket_validates_frames_and_acknowledges_eos(tmp_path, runtime_factory):
    app = create_worker_app(tmp_path, TOKEN, runtime_factory)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(app, headers=headers) as client:
        sid = client.post("/v1/sessions", json={"capture_tail_s": 0, "min_free_disk_mb": 1}).json()[
            "session_id"
        ]
        stream = client.post(
            f"/v1/sessions/{sid}/streams", json={"kind": "audio", "format": "S16LE"}
        ).json()["stream_id"]
        with client.websocket_connect(
            f"/v1/sessions/{sid}/streams/{stream}/ws", headers=headers
        ) as ws:
            assert ws.receive_json()["ready"]
            ws.send_bytes(HEADER.pack(0, 0) + bytes(1920))
            assert ws.receive_json()["accepted_sequence"] == 0
            ws.send_json({"end": True})
            assert ws.receive_json()["ended"]


async def test_auth_state_not_persisted_in_manifest(tmp_path, runtime_factory):
    app = create_worker_app(tmp_path, TOKEN, runtime_factory)
    async with Client("http://test", TOKEN, transport=httpx.ASGITransport(app=app)) as client:
        session = await client.start_session(
            SessionConfig(storage_state={"cookies": [{"value": "secret"}]})
        )
        assert "secret" not in (tmp_path / "manifest.json").read_text()
        await session.stop()


def test_new_browser_recording_and_binary_output_routes(tmp_path, runtime_factory):
    from mba.generated.adapter_pb2 import MediaFormat, MediaPacket

    class Runtime(runtime_factory):
        def __init__(self, streams):
            super().__init__(streams)
            self.browser = self

        async def command(self, **values):
            return values

        async def record(self, enabled):
            self.recording = enabled

        async def capture(self, kind, **options):
            yield MediaPacket(
                stream_id="speaker",
                sequence=7,
                pts_us=1000,
                data=bytes(1920),
                format=MediaFormat(kind="audio", encoding="S16LE", rate=48000, channels=1),
            )

    app = create_worker_app(tmp_path, TOKEN, Runtime)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    with TestClient(app, headers=headers) as client:
        sid = client.post("/v1/sessions", json={"capture_tail_s": 0, "min_free_disk_mb": 1}).json()[
            "session_id"
        ]
        base = f"/v1/sessions/{sid}"
        assert (
            client.post(
                base + "/browser",
                json={"operation": "fill", "selector": "textarea", "value": "hello"},
            ).json()["value"]
            == "hello"
        )
        assert client.post(base + "/browser", json={"operation": "invalid"}).status_code == 422
        assert client.post(base + "/recording", json={"enabled": True}).json() == {"enabled": True}
        with client.websocket_connect(base + "/capture/audio/ws?channels=1", headers=headers) as ws:
            packet = MediaPacket.FromString(ws.receive_bytes())
            assert packet.sequence == 7 and packet.data == bytes(1920)
