"""Real Linux audio/display acceptance, without requiring a kernel camera module.

Run inside the worker image as an unprivileged user. No fake media devices are used.
The local page is a measurement fixture, not a scenario or model simulator.
"""

import asyncio
import json
import math
import os
import subprocess
import wave
from array import array
from pathlib import Path

from mba import Adapter, AudioFormat, BindingConfig, SessionConfig
from mba.media import sine_pcm

PAGE = b"""<!doctype html><body style="background:rgb(220,30,40)">
<button id="join">Join</button><button id="tone">Tone</button><p id="status"></p>
<input id="composer"><button id="send">Send</button><div id="messages"></div>
<script>
let ctx, analyser, timer;
document.querySelector('#join').onclick=async()=>{try{
const stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:false,
noiseSuppression:false,autoGainControl:false}});
ctx=new AudioContext({sampleRate:48000});await ctx.resume();
analyser=ctx.createAnalyser();analyser.fftSize=2048;
ctx.createMediaStreamSource(stream).connect(analyser);
const data=new Float32Array(2048);
timer=setInterval(()=>{analyser.getFloatTimeDomainData(data);let crossings=0;
for(let i=1;i<data.length;i++)if(data[i-1]<=0&&data[i]>0)crossings++;
fetch(location.pathname+'/probe',{method:'POST',body:JSON.stringify({
rms:Math.sqrt(data.reduce((s,x)=>s+x*x,0)/data.length),hz:crossings*48000/2048})});},50);
document.querySelector('#status').textContent='Ready';document.querySelector('#status').id='ready';
}catch(e){document.querySelector('#status').textContent=String(e)}};
document.querySelector('#tone').onclick=()=>{const o=ctx.createOscillator(),g=ctx.createGain();
o.frequency.value=997;g.gain.value=.2;o.connect(g).connect(ctx.destination);
o.start();o.stop(ctx.currentTime+1)};
document.querySelector('#send').onclick=()=>{const i=document.querySelector('#composer');
const p=document.createElement('p');p.className='assistant';p.textContent=i.value;
document.querySelector('#messages').append(p);i.value=''};
</script></body>"""


async def main():
    root = Path(os.environ.get("MBA_SMOKE_ROOT", "/tmp/mba-smoke"))
    root.mkdir(parents=True, exist_ok=True)
    probes = []
    isolated_probes = {"/a/probe": [], "/b/probe": []}

    async def handle(reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            lines = header.decode().split("\r\n")
            if lines[0].startswith("POST "):
                length = next(
                    int(x.split(":", 1)[1])
                    for x in lines
                    if x.lower().startswith("content-length:")
                )
                target = isolated_probes.get(lines[0].split()[1], probes)
                target.append(json.loads(await reader.readexactly(length)))
                body = b"ok"
            else:
                body = PAGE
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    config = SessionConfig(
        mode="audio",
        camera=False,
        target_url=f"http://127.0.0.1:{port}/primary",
        binding=BindingConfig(
            kind="declarative", join_selector="#join", message_selector=".assistant"
        ),
    )
    try:
        async with Adapter(config, artifact_dir=root) as session:
            print("READY: real Rust/PulseAudio/Xvfb/TypeScript/Chromium session", flush=True)
            await (await session.text.send("container text round trip")).wait()
            await asyncio.sleep(0.2)
            assert any(
                e.type == "text.revision" and e.data["content"] == "container text round trip"
                for e in session.core.evidence.read(0)
            )

            async def capture():
                chunks = []
                source = session.audio.capture(channels=1)
                try:
                    async with asyncio.timeout(8):
                        async for chunk in source:
                            chunks.append(chunk.data)
                            samples = array("h", chunk.data)
                            if max(map(abs, samples), default=0) > 1000:
                                return chunks
                finally:
                    await source.aclose()

            output = asyncio.create_task(capture())
            await session.browser.click("#tone")
            chunks = await output
            assert chunks
            print("PASS: browser speaker reaches native PCM capture", flush=True)

            path = root / "input.wav"
            with wave.open(str(path), "wb") as wav:
                wav.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
                wav.writeframes(sine_pcm(2, 440))
            await (await session.audio.play(path)).wait()
            assert any(p["rms"] > 0.02 and abs(p["hz"] - 440) < 40 for p in probes), probes[-10:]
            print("PASS: WAV injection reaches browser microphone", flush=True)

            probes.clear()
            async with session.audio.open_input(AudioFormat(rate=24000)) as stream:
                for index in range(60):
                    values = array(
                        "h",
                        (
                            int(8000 * math.sin(2 * math.pi * 660 * (index * 480 + i) / 24000))
                            for i in range(480)
                        ),
                    )
                    await stream.write(values.tobytes())
            assert any(p["rms"] > 0.02 and abs(p["hz"] - 660) < 40 for p in probes), probes[-10:]
            print("PASS: live 24 kHz PCM resamples into browser microphone", flush=True)

            with wave.open(str(path), "wb") as wav:
                wav.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
                wav.writeframes(sine_pcm(10, 440))
            action = await session.audio.play(path)
            await asyncio.sleep(0.6)
            await action.cancel()
            await asyncio.sleep(0.3)
            probes.clear()
            await asyncio.sleep(0.3)
            assert probes and all(p["rms"] < 0.005 for p in probes), probes
            print("PASS: interruption returns browser microphone to silence", flush=True)

            frames = session.visual.frames(fps=1)
            try:
                frame = await asyncio.wait_for(anext(frames), 5)
                assert len(frame.data) == frame.format.width * frame.format.height * 3
                pixels = list(
                    zip(frame.data[0::96], frame.data[1::96], frame.data[2::96], strict=True)
                )
                assert sum(r > 180 and g < 80 and b < 100 for r, g, b in pixels) > len(pixels) / 4
            finally:
                await frames.aclose()
            assert (await session.browser.screenshot()).is_file()
            print("PASS: Xvfb display frames and screenshot capture", flush=True)
            manifest = await session.stop()
            assert manifest["state"] == "closed", manifest
            assert {"audio.segment", "video.segment", "video.recording", "frame"} <= {
                a["kind"] for a in manifest["artifacts"]
            }
            for artifact in manifest["artifacts"]:
                artifact_path = session.core.evidence.root / artifact["path"]
                if artifact_path.suffix in {".wav", ".webm"}:
                    await asyncio.to_thread(
                        subprocess.run,
                        ["ffmpeg", "-v", "error", "-i", str(artifact_path), "-f", "null", "-"],
                        check=True,
                    )
            print("PASS: finalized recordings decode and shutdown completes", flush=True)

        a_config = SessionConfig.model_validate(
            {
                **config.model_dump(),
                "target_url": f"http://127.0.0.1:{port}/a",
                "visual": False,
                "recording": False,
            }
        )
        b_config = SessionConfig.model_validate(
            {
                **config.model_dump(),
                "target_url": f"http://127.0.0.1:{port}/b",
                "visual": False,
                "recording": False,
            }
        )
        async with (
            Adapter(a_config, artifact_dir=root) as a,
            Adapter(b_config, artifact_dir=root) as b,
        ):
            with wave.open(str(path), "wb") as wav:
                wav.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
                wav.writeframes(sine_pcm(1, 440))
            await (await a.audio.play(path)).wait()
            assert any(p["rms"] > 0.02 for p in isolated_probes["/a/probe"])
            assert isolated_probes["/b/probe"] and all(
                p["rms"] < 0.005 for p in isolated_probes["/b/probe"]
            )
            assert a.core.runtime.media.socket != b.core.runtime.media.socket
        print("PASS: two concurrent native sessions isolate microphone audio", flush=True)
        await asyncio.sleep(0.2)
        leaked = []
        for process in Path("/proc").glob("[0-9]*/comm"):
            try:
                name = process.read_text().strip()
            except FileNotFoundError:
                continue
            if name in {"mba-media", "node", "chrome", "Xvfb", "openbox", "pulseaudio"}:
                leaked.append((process.parent.name, name))
        assert not leaked, leaked
        print("PASS: no browser, media, display, or audio processes remain", flush=True)
    finally:
        server.close()
        await server.wait_closed()


asyncio.run(main())
