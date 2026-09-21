import asyncio
import json

import pytest

from mba.evidence import Clock, Evidence
from mba.protocol import AdapterError


async def test_observation_time_and_replay_order_are_independent(tmp_path):
    evidence = Evidence(tmp_path, "s", Clock())
    await evidence.emit("later_observation", observed_at_us=1000)
    await evidence.emit("delayed_transcript", observed_at_us=100)
    events = evidence.read(0)
    assert [e.sequence for e in events] == [1, 2]
    assert [e.observed_at_us for e in events] == [1000, 100]
    assert evidence.read(1)[0].type == "delayed_transcript"
    await evidence.close()
    replay = [e async for e in evidence.events(0)]
    assert replay == events


async def test_slow_subscriber_does_not_hold_writer(tmp_path):
    evidence = Evidence(tmp_path, "s", Clock())
    stream = evidence.events()
    await evidence.emit("first")
    assert (await anext(stream)).sequence == 1
    await asyncio.wait_for(evidence.emit("second"), 0.2)
    assert (await anext(stream)).sequence == 2
    await stream.aclose()
    await evidence.close()


async def test_artifact_manifest_is_incremental_and_paths_cannot_escape(tmp_path):
    evidence = Evidence(tmp_path / "session", "s", Clock())
    audio = evidence.root / "recordings" / "one.wav"
    audio.write_bytes(b"test")
    await evidence.artifact(audio, "audio.segment", 0, 20_000)
    assert json.loads((evidence.root / "manifest.json").read_text())["artifacts"][0]["complete"]
    secret = tmp_path / "outside"
    secret.write_text("secret")
    with pytest.raises(AdapterError):
        evidence.resolve("../outside")
    (evidence.root / "link").symlink_to(secret)
    with pytest.raises(AdapterError):
        evidence.resolve("link")
    await evidence.close()


async def test_oversized_upload_removes_partial_file(tmp_path):
    evidence = Evidence(tmp_path, "s", Clock())

    async def chunks():
        yield b"1234"
        yield b"5678"

    with pytest.raises(AdapterError, match="exceeds"):
        await evidence.upload(chunks(), limit=4)
    assert not list((tmp_path / "assets").iterdir())
    await evidence.close()
