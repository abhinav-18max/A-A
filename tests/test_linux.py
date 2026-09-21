"""End-to-end acceptance against real Docker workers and host virtual devices.

Run only on the prepared Linux host: MBA_LINUX_TESTS=1 pytest tests/test_linux.py.
No fake devices or fake runtime are used in this module.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import secrets
import socket
import subprocess
import sys
import wave
from array import array
from pathlib import Path

import httpx
import pytest

from mba.media import sine_pcm
from mba.protocol import Action, AudioTrack, SessionConfig, VideoTrack
from mba.sdk import Client

pytestmark = [
    pytest.mark.linux,
    pytest.mark.skipif(
        os.environ.get("MBA_LINUX_TESTS") != "1" or platform.system() != "Linux",
        reason="requires opted-in prepared Linux Docker host",
    ),
]


@pytest.fixture
async def native(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    token = secrets.token_hex(32)
    root = tmp_path / "sessions"
    repo = Path(__file__).parents[1]
    log = (tmp_path / "controller.log").open("wb")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "mba.cli",
        "serve",
        "--port",
        str(port),
        "--root",
        str(root),
        "--seccomp",
        str(repo / "deploy/seccomp.json"),
        env={**os.environ, "MBA_TOKEN": token},
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    async with Client(f"http://127.0.0.1:{port}", token) as client:
        try:
            async with asyncio.timeout(20):
                while True:
                    if process.returncode is not None:
                        raise RuntimeError((tmp_path / "controller.log").read_text())
                    try:
                        await client.request("GET", "/health")
                        break
                    except httpx.HTTPError:
                        await asyncio.sleep(0.1)
            yield client, root
        finally:
            try:
                rows = (await client.request("GET", "/v1/sessions")).json()
                for row in rows:
                    await client.session(row["session_id"]).stop()
            finally:
                if process.returncode is None:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), 10)
                log.close()


def audio_fixture(path, hz=440, duration=1):
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        output.writeframes(sine_pcm(duration, hz))
    return path


def video_fixture(path, color="red", duration=1):
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=1280x720:r=30",
            "-t",
            str(duration),
            "-c:v",
            "libvpx",
            "-deadline",
            "realtime",
            "-y",
            str(path),
        ],
        check=True,
    )
    return path


async def calibration(session, command=None):
    method = "POST" if command else "GET"
    kwargs = {"json": command} if command else {}
    return (await session.client.request(method, session.path + "/calibration", **kwargs)).json()


async def test_real_full_duplex_audio_video_text_and_artifacts(native, tmp_path):
    client, root = native
    session = await client.start_session(SessionConfig(capture_tail_s=0.2))
    await (await session.send_text("full multimodal test")).wait()
    audio = await session.upload(audio_fixture(tmp_path / "input.wav", 440, 2))
    video = await session.upload(video_fixture(tmp_path / "input.webm", "red", 2))
    handle = await session.submit(
        Action(
            action_id="bundle",
            tracks=[
                AudioTrack(source={"kind": "asset", "asset_id": audio}),
                VideoTrack(source={"kind": "asset", "asset_id": video}),
            ],
        )
    )
    await calibration(session, {"kind": "tone", "frequency": 997, "duration_ms": 500})
    await handle.wait()
    probe = await calibration(session)
    assert probe["peerReady"]
    assert any(row["rms"] > 0.02 and abs(row["hz"] - 440) < 40 for row in probe["audio"])
    assert any(row["rgb"][0] > 150 and row["rgb"][1] < 80 for row in probe["cameraFrames"])
    await session.checkpoint()
    manifest = await session.stop()
    assert manifest["state"] == "closed", manifest
    artifacts = manifest["artifacts"]
    assert {"audio.segment", "video.segment", "frame"} <= {a["kind"] for a in artifacts}
    speaker = [a for a in artifacts if a.get("stream") == "speaker"]
    assert speaker
    peak = 0
    for recording in speaker:
        with wave.open(str(root / session.id / recording["path"]), "rb") as source:
            samples = array("h", source.readframes(source.getnframes()))
            peak = max(peak, max(map(abs, samples), default=0))
    assert peak > 500, "known browser response was not captured at the speaker"
    for artifact in artifacts:
        path = root / session.id / artifact["path"]
        assert path.stat().st_size > 0
        if path.suffix in {".webm", ".wav"}:
            await asyncio.to_thread(
                subprocess.run,
                ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
                check=True,
            )
    events = await session.event_page()
    assert any(
        e.type == "text.revision" and "full multimodal test" in e.data["content"] for e in events
    )


async def test_two_real_workers_isolate_audio_and_camera(native, tmp_path):
    client, _ = native
    a, b = await asyncio.gather(client.start_session(), client.start_session())
    aa = await a.upload(audio_fixture(tmp_path / "a.wav", 440, 2))
    ba = await b.upload(audio_fixture(tmp_path / "b.wav", 997, 2))
    av = await a.upload(video_fixture(tmp_path / "a.webm", "red", 2))
    bv = await b.upload(video_fixture(tmp_path / "b.webm", "blue", 2))
    actions = []
    for session, audio, video in ((a, aa, av), (b, ba, bv)):
        actions.append(
            await session.submit(
                Action(
                    tracks=[
                        AudioTrack(source={"kind": "asset", "asset_id": audio}),
                        VideoTrack(source={"kind": "asset", "asset_id": video}),
                    ]
                )
            )
        )
    await asyncio.gather(*(action.wait() for action in actions))
    pa, pb = await asyncio.gather(calibration(a), calibration(b))
    for probe, desired, unwanted in ((pa, 440, 997), (pb, 997, 440)):
        audible = [r for r in probe["audio"] if r["rms"] > 0.02]
        assert any(abs(r["hz"] - desired) < 40 for r in audible)
        assert not any(abs(r["hz"] - unwanted) < 40 for r in audible)
    assert any(r["rgb"][0] > 150 and r["rgb"][2] < 80 for r in pa["cameraFrames"])
    assert not any(r["rgb"][2] > 150 and r["rgb"][0] < 80 for r in pa["cameraFrames"])
    assert any(r["rgb"][2] > 150 and r["rgb"][0] < 80 for r in pb["cameraFrames"])
    await asyncio.gather(a.stop(), b.stop())


async def test_real_cancellation_returns_microphone_to_silence(native, tmp_path):
    client, _ = native
    session = await client.start_session()
    action = await session.send_audio(audio_fixture(tmp_path / "long.wav", 440, 10))
    await asyncio.sleep(0.7)
    before = await calibration(session)
    await action.cancel()
    await asyncio.sleep(0.4)
    after = await calibration(session)
    tail = [s for s in after["audio"] if s["at"] > before["now"] + 200]
    assert tail and all(row["rms"] < 0.005 for row in tail)
    assert (await action.status()).state == "cancelled"
    await session.stop()


@pytest.mark.soak
@pytest.mark.skipif(
    os.environ.get("MBA_SOAK_TESTS") != "1",
    reason="set MBA_SOAK_TESTS=1 for 30-minute and 100-cycle acceptance",
)
async def test_two_worker_soak_and_repeated_cleanup(native, tmp_path):
    client, root = native
    a, b = await asyncio.gather(
        client.start_session(SessionConfig(max_duration_s=1900)),
        client.start_session(SessionConfig(max_duration_s=1900)),
    )
    for index in range(180):
        await asyncio.gather(
            (await a.send_text(f"soak-{index}")).wait(), (await b.send_text(f"soak-{index}")).wait()
        )
        await asyncio.sleep(10)
    manifests = await asyncio.gather(a.stop(), b.stop())
    assert all(m["state"] == "closed" for m in manifests)
    for manifest in manifests:
        metrics = manifest["metrics"]
        total = metrics["display_frames"] + metrics["display_gap_frames"]
        assert total > 0 and metrics["display_gap_frames"] / total <= 0.01
    for session in (a, b):
        after = 0
        while batch := await session.event_page(after):
            assert not any(e.type in {"capture.gap", "media.error"} for e in batch)
            after = batch[-1].sequence
    for _ in range(100):
        session = await client.start_session(SessionConfig(capture_tail_s=0))
        assert (await session.stop())["state"] == "closed"
    running = await asyncio.to_thread(
        subprocess.check_output,
        ["docker", "ps", "--filter", "label=mba.managed=true", "--format", "{{.Names}}"],
        text=True,
    )
    registry = json.loads((root / "registry.json").read_text())
    assert all("mba-" + sid not in running for sid in registry)


async def test_measured_audio_onset_and_av_skew(native, tmp_path):
    client, root = native
    session = await client.start_session(SessionConfig(max_duration_s=300))
    audio = await session.upload(audio_fixture(tmp_path / "timing.wav", 440, 0.5))
    video = await session.upload(video_fixture(tmp_path / "timing.webm", "red", 0.5))
    measurements = []
    for index in range(20):
        probe = await calibration(session)
        start_us = int(probe["now"] * 1000 + probe["clock_offset_us"] + 2_000_000)
        action = await session.submit(
            Action(
                action_id=f"timing-{index}",
                start_at_us=start_us,
                tracks=[
                    AudioTrack(source={"kind": "asset", "asset_id": audio}),
                    VideoTrack(source={"kind": "asset", "asset_id": video}),
                ],
            )
        )
        await action.wait()
        probe = await calibration(session)
        assert probe["clock_uncertainty_us"] < 10_000
        offset = probe["clock_offset_us"]
        audio_onset = min(
            r["at"] * 1000 + offset
            for r in probe["audio"]
            if r["rms"] > 0.02 and r["at"] * 1000 + offset >= start_us - 10_000
        )
        video_onset = min(
            r["at"] * 1000 + offset
            for r in probe["cameraFrames"]
            if r["rgb"][0] > 150
            and r["rgb"][1] < 80
            and r["at"] * 1000 + offset >= start_us - 10_000
        )
        measurements.append(
            {
                "audio_onset_us": audio_onset - start_us,
                "av_skew_us": abs(video_onset - audio_onset),
                "clock_uncertainty_us": probe["clock_uncertainty_us"],
            }
        )
    report = {
        "samples": measurements,
        "audio_p95_us": sorted(r["audio_onset_us"] for r in measurements)[18],
        "av_skew_p95_us": sorted(r["av_skew_us"] for r in measurements)[18],
    }
    (root / session.id / "timing-report.json").write_text(json.dumps(report, indent=2))
    assert report["audio_p95_us"] <= 150_000, report
    assert report["av_skew_p95_us"] <= 100_000, report
    await session.stop()
