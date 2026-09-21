"""Interactive two-browser audio test; no provider API or stored login credentials.

Run in the optional noVNC worker image. Sign in through the localhost viewer.
The host writes command.json with {"operation": "connect"} only after Voice is
ready. Native GStreamer relays carry media without Python processing PCM packets.
"""

import asyncio
import json
import os
import secrets
import signal
import subprocess
import time
from contextlib import AsyncExitStack
from pathlib import Path

from mba import Adapter, SessionConfig

ROOT = Path(os.environ.get("MBA_PAIR_ROOT", "/artifacts"))
PAGE = b"""<!doctype html><button id="join">Join</button><button id="tone">Tone</button>
<script>
let ctx; document.querySelector('#join').onclick=async()=>{
ctx=new AudioContext({sampleRate:48000});await ctx.resume();
let s=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:false,
noiseSuppression:false,autoGainControl:false}}),a=ctx.createAnalyser();a.fftSize=2048;
ctx.createMediaStreamSource(s).connect(a);let b=new Float32Array(2048);
setInterval(()=>{a.getFloatTimeDomainData(b);let z=0;
for(let i=1;i<b.length;i++)if(b[i-1]<=0&&b[i]>0)z++;
fetch(location.pathname+'/probe',{method:'POST',body:JSON.stringify({
rms:Math.sqrt(b.reduce((s,x)=>s+x*x,0)/b.length),hz:z*48000/2048})});},50);
document.querySelector('#join').id='ready';};
document.querySelector('#tone').onclick=()=>{let o=ctx.createOscillator(),g=ctx.createGain();
o.frequency.value=location.pathname==='/a'?440:880;g.gain.value=.2;
o.connect(g).connect(ctx.destination);o.start();o.stop(ctx.currentTime+2);};
</script>"""


def status(state, **details):
    data = {"state": state, "time": time.time(), **details}
    path = ROOT / "status.tmp"
    path.write_text(json.dumps(data, indent=2))
    path.replace(ROOT / "status.json")
    print(json.dumps(data), flush=True)


async def stop_process(process):
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            process.kill()
            await process.wait()


async def spawn(stack, name, *args):
    with (ROOT / f"{name}.log").open("ab") as log:
        process = await asyncio.create_subprocess_exec(*args, stdout=log, stderr=log)
    stack.push_async_callback(stop_process, process)
    await asyncio.sleep(0.1)
    if process.returncode is not None:
        raise RuntimeError(f"{name} exited; see its log")
    return process


async def relays(stack, left, right):
    processes = []
    for name, source, destination in (
        ("chatgpt-to-docket", left, right),
        ("docket-to-chatgpt", right, left),
    ):
        src = source.core.runtime.browser.environment["PULSE_SERVER"]
        dst = destination.core.runtime.browser.environment["PULSE_SERVER"]
        processes.append(
            await spawn(
                stack,
                name,
                "gst-launch-1.0",
                "-e",
                "pulsesrc",
                f"server={src}",
                "device=mba_out.monitor",
                "buffer-time=40000",
                "latency-time=10000",
                "!",
                "audioconvert",
                "!",
                "audioresample",
                "!",
                "audio/x-raw,format=S16LE,rate=48000,channels=1",
                "!",
                "queue",
                "max-size-buffers=10",
                "max-size-bytes=0",
                "max-size-time=0",
                "!",
                "pulsesink",
                f"server={dst}",
                "device=mba_in",
                "buffer-time=40000",
                "latency-time=10000",
                "sync=false",
            )
        )
    return processes


async def preflight(left, right):
    probes = {"/a/probe": [], "/b/probe": []}

    async def handle(reader, writer):
        try:
            headers = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            if headers[0].startswith("POST "):
                length = next(
                    int(x.split(":", 1)[1])
                    for x in headers
                    if x.lower().startswith("content-length:")
                )
                probes[headers[0].split()[1]].append(json.loads(await reader.readexactly(length)))
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

    server = await asyncio.start_server(handle, "127.0.0.1", 8765)
    try:
        port = server.sockets[0].getsockname()[1]
        # localhost is already a configured microphone permission origin.
        for path, session in (("a", left), ("b", right)):
            await session.browser.open(f"http://127.0.0.1:{port}/{path}")
            await session.browser.click("#join")
            await session.browser.wait_for("#ready")
        async with AsyncExitStack() as stack:
            await relays(stack, left, right)
            await asyncio.sleep(3)
            probes["/a/probe"].clear()
            probes["/b/probe"].clear()
            await left.browser.click("#tone")
            await asyncio.sleep(3)
            forward = any(p["rms"] > 0.02 and abs(p["hz"] - 440) < 40 for p in probes["/b/probe"])
            self_echo = any(p["rms"] > 0.02 for p in probes["/a/probe"])
            probes["/a/probe"].clear()
            probes["/b/probe"].clear()
            await right.browser.click("#tone")
            await asyncio.sleep(3)
            reverse = any(p["rms"] > 0.02 and abs(p["hz"] - 880) < 40 for p in probes["/a/probe"])
            self_echo |= any(p["rms"] > 0.02 for p in probes["/b/probe"])
        result = {
            "forward_microphone": forward,
            "reverse_microphone": reverse,
            "self_echo": self_echo,
        }
        (ROOT / "preflight.json").write_text(json.dumps(result, indent=2))
        if not forward or not reverse or self_echo:
            raise RuntimeError(f"Audio relay verification failed: {result}")
        status("audio_preflight_passed", **result)
    finally:
        # Leave fixture pages before closing their probe server. This also releases
        # their microphone tracks and avoids stale fetches during site navigation.
        await asyncio.gather(left.browser.open("about:blank"), right.browser.open("about:blank"))
        server.close()
        await server.wait_closed()


async def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    for stale in ("command.json", "status.json"):
        (ROOT / stale).unlink(missing_ok=True)
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    status("starting")
    async with AsyncExitStack() as stack:
        # Neither video nor audio is recorded while signing in.
        config = SessionConfig(
            mode="audio",
            camera=False,
            visual=True,
            permission_origins=[
                "http://127.0.0.1:8765",
                "https://chatgpt.com",
                "https://www.docket.io",
            ],
            startup_timeout_s=90,
            action_timeout_s=240,
        )
        left = await stack.enter_async_context(Adapter(config, artifact_dir=ROOT / "chatgpt"))
        right = await stack.enter_async_context(Adapter(config, artifact_dir=ROOT / "docket"))
        await preflight(left, right)
        password = secrets.token_hex(4)
        password_path = ROOT / "viewer-password.txt"
        password_path.write_text(password)
        password_path.chmod(0o600)
        await asyncio.to_thread(
            subprocess.run,
            ["x11vnc", "-storepasswd", password, "/tmp/pair-vnc.pass"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for index, session in enumerate((left, right)):
            display = session.core.runtime.browser.environment["DISPLAY"]
            await spawn(
                stack,
                f"vnc-{index}",
                "x11vnc",
                "-display",
                display,
                "-localhost",
                "-rfbport",
                str(5900 + index),
                "-forever",
                "-shared",
                "-rfbauth",
                "/tmp/pair-vnc.pass",
                "-noxdamage",
            )
            await spawn(
                stack,
                f"viewer-{index}",
                "/usr/bin/websockify",
                "--web=/usr/share/novnc",
                f"0.0.0.0:{6080 + index}",
                f"127.0.0.1:{5900 + index}",
            )
        for session, url in ((left, "https://chatgpt.com/"), (right, "https://www.docket.io/")):
            try:
                await session.core.runtime.browser.command("open", value=url, timeout_ms=45000)
            except Exception as error:
                # Keep the viewer available even if a site's load event is delayed.
                status("navigation_incomplete", url=url, error=str(error))
        status("awaiting_chatgpt_login_and_voice", expires_in_seconds=1800)
        deadline = time.monotonic() + 1800
        while not stop.is_set() and time.monotonic() < deadline:
            command_path = ROOT / "command.json"
            if command_path.exists():
                command = json.loads(command_path.read_text())
                command_path.unlink()
                operation = command.get("operation")
                if operation == "stop":
                    break
                if operation == "connect":
                    await left.recording.start()
                    await right.recording.start()
                    async with AsyncExitStack() as routes:
                        processes = await relays(routes, left, right)
                        try:
                            await right.browser.click('text="Deny"', timeout_ms=3000)
                        except Exception:
                            pass  # Cookie banner can have been dismissed through the viewer.
                        await right.browser.click('text="Talk to Aura"', timeout_ms=15000)
                        status("connected", duration_seconds=180)
                        end = time.monotonic() + 180
                        while time.monotonic() < end and not stop.is_set():
                            if any(p.returncode is not None for p in processes):
                                raise RuntimeError("Native audio relay exited")
                            if command_path.exists():
                                if json.loads(command_path.read_text()).get("operation") == "stop":
                                    stop.set()
                            await asyncio.sleep(0.25)
                    break
            await asyncio.sleep(0.25)
        # Closing the browsers ends both remote calls and flushes recordings.
    status("closed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        status("failed", error=str(error))
        raise
