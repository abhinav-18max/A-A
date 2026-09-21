"""External Python harness -> Linux browser + real audio/display devices.

No model, login, or conversation policy runs in the worker. The page below only
measures the device routes. Run from the Mac/host, not inside Docker.
"""

import argparse
import asyncio
import json
import math
import os
import secrets
import statistics
import sys
import time
import wave
from array import array
from pathlib import Path

import httpx

from mba import AudioFormat, SessionConfig
from mba.controller import DockerHost
from mba.protocol import new_id
from mba.sdk import Client

PAGE = """<body style="background:rgb(220,30,40)"><button id="join">Join</button>
<button id="tone">Tone</button><p id="status"></p><script>
window.probes=[];let ctx,analyser;
document.querySelector('#join').onclick=async()=>{
const stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:false,noiseSuppression:false,autoGainControl:false}});
ctx=new AudioContext({sampleRate:48000});await ctx.resume();analyser=ctx.createAnalyser();analyser.fftSize=2048;
ctx.createMediaStreamSource(stream).connect(analyser);const data=new Float32Array(2048);
setInterval(()=>{analyser.getFloatTimeDomainData(data);let crossings=0;for(let i=1;i<data.length;i++)if(data[i-1]<=0&&data[i]>0)crossings++;
window.probes.push({rms:Math.sqrt(data.reduce((s,x)=>s+x*x,0)/data.length),hz:crossings*48000/2048});},50);
document.querySelector('#status').textContent='Ready';};
document.querySelector('#tone').onclick=()=>{const o=ctx.createOscillator(),g=ctx.createGain();o.frequency.value=997;g.gain.value=.2;o.connect(g).connect(ctx.destination);o.start();o.stop(ctx.currentTime+1);};
document.modelContext?.registerTool({name:'probe_count',description:'Microphone probes so far',
inputSchema:{type:'object',properties:{}},execute:async()=>String(window.probes.length)});
</script>"""


async def main(image):
    repo = Path(__file__).resolve().parents[1]
    root = repo / ".cache/harness-container-artifacts"
    root.mkdir(parents=True, exist_ok=True)
    host = DockerHost(root, image, repo / "deploy/seccomp.json")
    sid, token = new_id(), secrets.token_hex(32)
    report = {"session_id": sid, "image": image, "checks": []}
    try:
        worker = await host.launch(sid, None, token)
        async with Client(worker["url"], token) as client:
            async with asyncio.timeout(30):
                while True:
                    try:
                        await client.request("GET", "/health")
                        break
                    except httpx.HTTPError:
                        await asyncio.sleep(0.1)
            session = await client.start_session(
                SessionConfig(
                    browser_control="native",
                    mode="audio",
                    camera=False,
                    webmcp=True,
                    permission_origins=["https://fixture.invalid"],
                )
            )
            async with session.connect_native() as native:
                await native.page.route(
                    "https://fixture.invalid/**",
                    lambda route: route.fulfill(body=PAGE, content_type="text/html"),
                )
                await native.page.goto("https://fixture.invalid/")
                await native.page.locator("#join").click()
                await native.page.get_by_text("Ready", exact=True).wait_for()
                report["checks"].append("external native browser setup and microphone permission")
                tools = await native.webmcp_tools(native.page)
                assert [t["name"] for t in tools["tools"]] == ["probe_count"], tools
                called = await native.webmcp_call(native.page, "probe_count")
                assert called["status"] == "completed" and int(called["result"]) >= 0, called
                report["checks"].append(
                    "WebMCP page tool listed and called in headed Linux Chromium"
                )
                samples = []
                for _ in range(20):
                    started = time.monotonic()
                    assert await native.page.evaluate("1 + 1") == 2
                    samples.append((time.monotonic() - started) * 1000)
                report["native_evaluate_rtt_ms"] = {
                    "samples": len(samples),
                    "p50": statistics.median(samples),
                    "p95": sorted(samples)[math.ceil(0.95 * len(samples)) - 1],
                }

                async def capture():
                    source = session.audio.capture(channels=1)
                    peak = 0
                    try:
                        with wave.open(str(root / "speaker.wav"), "wb") as output:
                            output.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
                            async with asyncio.timeout(10):
                                async for chunk in source:
                                    output.writeframesraw(chunk.data)
                                    peak = max(
                                        peak, max(map(abs, array("h", chunk.data)), default=0)
                                    )
                                    if peak > 1000:
                                        return peak
                    finally:
                        await source.aclose()

                output = asyncio.create_task(capture())
                await native.page.locator("#tone").click()
                report["speaker_peak"] = await output
                report["checks"].append("browser speaker audio captured by external SDK")

                # A real external MCP client shares media access without owning Playwright.
                from mcp import ClientSession, StdioServerParameters
                from mcp.client.stdio import stdio_client

                params = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "mba.cli", "mcp", "--worker-url", worker["url"]],
                    env={**os.environ, "MBA_TOKEN": token},
                )
                async with (
                    stdio_client(params) as (read, write),
                    ClientSession(read, write) as mcp,
                ):
                    await mcp.initialize()
                    attached = await mcp.call_tool("aa_session_attach", {"session_id": session.id})
                    assert not attached.isError, attached
                    capture_job = await mcp.call_tool(
                        "aa_audio_capture", {"session_id": session.id, "duration_s": 2}
                    )
                    assert not capture_job.isError, capture_job
                    await asyncio.sleep(0.25)
                    await native.page.locator("#tone").click()
                    async with asyncio.timeout(10):
                        while True:
                            status = await mcp.call_tool(
                                "aa_action_status",
                                {
                                    "session_id": session.id,
                                    "action_id": capture_job.structuredContent["action_id"],
                                },
                            )
                            assert not status.isError, status
                            if status.structuredContent["state"] != "started":
                                break
                            await asyncio.sleep(0.1)
                    assert status.structuredContent["state"] == "completed", status
                    artifact = await mcp.call_tool(
                        "aa_artifact_read",
                        {"artifact_id": status.structuredContent["artifact"]["artifact_id"]},
                    )
                    assert not artifact.isError, artifact
                    import base64
                    import io

                    audio = next(c for c in artifact.content if c.type == "audio")
                    assert audio.mimeType == "audio/wav"
                    assert "base64" not in artifact.structuredContent
                    with wave.open(io.BytesIO(base64.b64decode(audio.data)), "rb") as wav:
                        assert wav.getframerate() == 48000
                        assert wav.getnframes() == 2 * 48000
                        assert max(map(abs, array("h", wav.readframes(wav.getnframes())))) > 1000
                assert (await session.capabilities())["native_connected"]
                assert await native.page.evaluate("1 + 1") == 2
                report["checks"].append(
                    "external MCP captures WAV as audio content; detaching preserves native controller"
                )

                # Stereo 24 kHz exercises the expanded remote format contract.
                async with session.audio.open_input(AudioFormat(rate=24000, channels=2)) as stream:
                    for index in range(75):
                        pcm = array("h")
                        for sample in range(480):
                            value = int(
                                8000 * math.sin(2 * math.pi * 660 * (index * 480 + sample) / 24000)
                            )
                            pcm.extend((value, value))
                        await stream.write(pcm.tobytes())
                probes = await native.page.evaluate("window.probes")
                assert any(p["rms"] > 0.02 and abs(p["hz"] - 660) < 40 for p in probes), probes[
                    -10:
                ]
                report["checks"].append("external stereo 24 kHz PCM reaches browser microphone")

                # The TypeScript client drives the same media routes as the Python SDK.
                await native.page.evaluate("window.probes=[]")
                node = await asyncio.create_subprocess_exec(
                    "node",
                    str(repo / "harness-client/tests/media-roundtrip.mjs"),
                    env={
                        **os.environ,
                        "MBA_URL": worker["url"],
                        "MBA_TOKEN": token,
                        "SESSION_ID": session.id,
                    },
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                async with asyncio.timeout(60):
                    assert (await node.stdout.readline()).strip() == b"INJECTED"
                    probes = await native.page.evaluate("window.probes")
                    assert any(p["rms"] > 0.02 and abs(p["hz"] - 880) < 40 for p in probes), probes[
                        -10:
                    ]
                    assert (await node.stdout.readline()).strip() == b"CAPTURING"
                    await native.page.locator("#tone").click()
                    stdout, stderr = await node.communicate()
                assert node.returncode == 0, stderr.decode()
                typescript = json.loads(stdout.decode().splitlines()[-1])
                frame = typescript["frame"]
                assert typescript["speaker_peak"] > 1000, typescript
                assert frame["bytes"] == frame["width"] * frame["height"] * 3, typescript
                report["typescript_client"] = typescript
                report["checks"].append(
                    "TypeScript client: 16 kHz microphone input, speaker capture and RGB frames"
                )

                async with session.audio.open_input() as stream:
                    for index in range(20):
                        pcm = array(
                            "h",
                            (
                                int(8000 * math.sin(2 * math.pi * 440 * (index * 960 + i) / 48000))
                                for i in range(960)
                            ),
                        )
                        await stream.write(pcm.tobytes())
                    started = time.monotonic()
                    await stream.cancel()
                    report["cancel_ack_ms"] = (time.monotonic() - started) * 1000
                await asyncio.sleep(0.3)
                await native.page.evaluate("window.probes=[]")
                await asyncio.sleep(0.3)
                probes = await native.page.evaluate("window.probes")
                assert probes and all(p["rms"] < 0.005 for p in probes), probes
                report["checks"].append("remote input cancellation returns microphone to silence")

                frames = session.visual.frames(fps=1)
                try:
                    frame = await asyncio.wait_for(anext(frames), 10)
                    assert len(frame.data) == frame.format.width * frame.format.height * 3
                finally:
                    await frames.aclose()
                report["checks"].append("external SDK receives rendered RGB frames")
            manifest = await session.inspect()
            assert manifest["state"] == "closed", manifest
            assert {"audio.segment", "video.segment"} <= {a["kind"] for a in manifest["artifacts"]}
            full = next(a for a in manifest["artifacts"] if a["kind"] == "video.recording")
            assert full["audio_streams"] == ["speaker", "injected"]
            await session.download(full["path"], root / "session.webm")
            file = "/artifacts/" + full["path"]
            probe = json.loads(
                await host.command(
                    "exec",
                    "mba-" + sid,
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_type,codec_name:format=duration",
                    "-of",
                    "json",
                    file,
                )
            )
            assert {s["codec_name"] for s in probe["streams"]} == {"vp8", "opus"}, probe
            assert float(probe["format"]["duration"]) > 4, probe
            await host.command(
                "exec",
                "mba-" + sid,
                "ffmpeg",
                "-v",
                "error",
                "-i",
                file,
                "-f",
                "null",
                "-",
            )
            report["full_recording"] = {"path": full["path"], **probe}
            report["checks"].append("default combined screen/audio recording downloads and decodes")
            report["artifact_count"] = len(manifest["artifacts"])
            events = await session.event_page()
            report["capture_gaps"] = [e.data for e in events if e.type == "capture.gap"]
            report["shutdown_events"] = [
                e.data for e in events if e.type == "browser.shutdown_forced"
            ]
            report["checks"].append("recordings finalized and session closed")
            leaked = await host.command(
                "exec",
                "mba-" + sid,
                "python",
                "-c",
                "from pathlib import Path; import json; "
                "names={'node','chrome','chromium','chrome-headless','headless_shell',"
                "'mba-media','Xvfb','openbox','pulseaudio'}; "
                "print(json.dumps([p.parent.name+':'+p.read_text().strip() "
                "for p in Path('/proc').glob('[0-9]*/comm') if p.read_text().strip() in names]))",
            )
            assert json.loads(leaked) == [], leaked
            report["checks"].append("no browser, media, or virtual-device child processes remain")
    finally:
        await host.remove(sid)
    assert await host.inspect("mba-" + sid) is None
    report["checks"].append("worker removed")
    (root / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="mba-worker:harness-arm64")
    asyncio.run(main(parser.parse_args().image))
