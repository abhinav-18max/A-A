"""External, interactive harness for a recorded browser voice conversation.

Run on macOS with the remote/native extras, Docker Desktop, say, and FFmpeg.
Send one JSON command per stdin line: inspect, click, evaluate, speak, wait, stop.
The caller supplies every utterance; no model or conversation policy runs in A&A.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import secrets
import time
from array import array
from pathlib import Path

import httpx

from mba import SessionConfig
from mba.controller import DockerHost
from mba.protocol import new_id
from mba.sdk import Client

SNAPSHOT = r"""() => {
  const roots=[document]; const shadows=[];
  for(let i=0;i<roots.length;i++) for(const e of roots[i].querySelectorAll('*'))
    if(e.shadowRoot) {
      roots.push(e.shadowRoot);
      const text=[...e.shadowRoot.children].filter(c=>!['STYLE','SCRIPT'].includes(c.tagName))
        .map(c=>c.innerText||'').join('\n');
      shadows.push({host:e.tagName+'#'+e.id,text});
    }
  const controls=roots.flatMap(r=>[...r.querySelectorAll('button,a,input,textarea,[role="button"]')])
    .filter(e=>e.getBoundingClientRect().width && e.getBoundingClientRect().height)
    .map(e=>({tag:e.tagName,id:e.id,text:e.innerText?.slice(0,150),label:e.getAttribute('aria-label'),title:e.title,type:e.type}));
  return {text:document.body?.innerText,shadows,controls};
}"""


class Harness:
    def __init__(self, root):
        self.root = root
        self.started = time.monotonic()
        self.last_active = 0.0
        self.speech_activity = asyncio.Event()
        self.audio_chunks = self.audio_active_chunks = self.audio_peak = 0
        self.last_audio_pts_us = 0
        self.audio_task = None
        self.turn = self.snapshot_index = 0
        self.transcript = []

    def log(self, kind, **data):
        row = {"kind": kind, "host_relative_s": round(time.monotonic() - self.started, 3), **data}
        with (self.root / "harness-events.jsonl").open("a") as out:
            out.write(json.dumps(row) + "\n")
        return row

    async def capture(self):
        source = self.session.audio.capture(channels=1)
        try:
            async for chunk in source:
                values = array("h", chunk.data)
                peak = max(map(abs, values), default=0)
                rms = math.sqrt(sum(v * v for v in values) / max(1, len(values)))
                self.audio_chunks += 1
                self.last_audio_pts_us = chunk.pts_us
                self.audio_peak = max(self.audio_peak, peak)
                if rms > 180:
                    self.audio_active_chunks += 1
                    self.last_active = time.monotonic()
                    self.speech_activity.set()
        finally:
            await source.aclose()

    async def snapshot(self):
        self.snapshot_index += 1
        frames = []
        for index, frame in enumerate(self.native.page.frames):
            try:
                frames.append({"index": index, "url": frame.url, **await frame.evaluate(SNAPSHOT)})
            except Exception as exc:
                frames.append({"index": index, "error": str(exc)})
        path = self.root / f"screen-{self.snapshot_index:02d}.png"
        await self.native.page.screenshot(path=str(path))
        result = {
            "frames": frames,
            "screenshot": str(path),
            "audio": {
                "chunks": self.audio_chunks,
                "active_chunks": self.audio_active_chunks,
                "peak": self.audio_peak,
                "last_pts_us": self.last_audio_pts_us,
            },
        }
        (self.root / f"snapshot-{self.snapshot_index:02d}.json").write_text(
            json.dumps(result, indent=2)
        )
        self.log("observation", snapshot=self.snapshot_index, screenshot=str(path))
        # Preserve full DOM observations on disk; keep terminal responses bounded.
        for f in frames:
            if "text" in f:
                f["text"] = (f["text"] or "")[-14000:]
            for shadow in f.get("shadows", []):
                shadow["text"] = (shadow["text"] or "")[-16000:]
        return result

    async def synthesize(self, text, suffix):
        base = self.root / "inputs" / suffix
        base.parent.mkdir(exist_ok=True)
        base.with_suffix(".txt").write_text(text)
        commands = [
            [
                "/usr/bin/say",
                "-v",
                "Samantha",
                "-r",
                "175",
                "-f",
                str(base.with_suffix(".txt")),
                "-o",
                str(base.with_suffix(".aiff")),
            ],
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(base.with_suffix(".aiff")),
                "-ar",
                "48000",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(base.with_suffix(".wav")),
            ],
        ]
        for argv in commands:
            proc = await asyncio.create_subprocess_exec(*argv, stderr=asyncio.subprocess.PIPE)
            _, error = await proc.communicate()
            if proc.returncode:
                raise RuntimeError(error.decode())
        return base.with_suffix(".wav")

    async def speak_file(self, text, path):
        row = self.log(
            "harness.utterance",
            text=text,
            source="external_tts",
            input=str(path),
            speaker_capture_pts_us=self.last_audio_pts_us,
        )
        self.transcript.append(row)
        handle = await self.session.send_audio(path)
        result = await handle.wait()
        self.log(
            "microphone.completed",
            action_id=handle.id,
            status=result.model_dump(mode="json"),
            speaker_capture_pts_us=self.last_audio_pts_us,
        )
        return time.monotonic()

    async def wait_response(self, after, wait_s=60):
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            if self.audio_task.done():
                self.audio_task.result()
                raise RuntimeError("speaker capture ended")
            now = time.monotonic()
            if self.last_active > after and now - self.last_active > 2.5 and now - after > 4:
                return "audio_became_quiet"
            await asyncio.sleep(0.2)
        return "response_wait_timeout"

    async def command(self, command):
        op = command["op"]
        page = self.native.page
        frame = page.frames[command.get("frame", 0)]
        if op == "inspect":
            return await self.snapshot()
        if op == "evaluate":
            return await frame.evaluate(command["script"])
        if op == "click":
            if "selector" in command:
                locator = frame.locator(command["selector"])
            elif "role" in command:
                locator = frame.get_by_role(command["role"], name=command["name"], exact=True)
            else:
                locator = frame.get_by_text(command["text"], exact=True)
            await locator.nth(command.get("nth", 0)).click(timeout=15000)
            await asyncio.sleep(command.get("settle_s", 1))
            return await self.snapshot()
        if op == "wait":
            await asyncio.sleep(min(60, command.get("seconds", 5)))
            return await self.snapshot()
        if op == "speak":
            self.turn += 1
            text = command["text"]
            path = await self.synthesize(text, f"turn-{self.turn:02d}")
            clarification = command.get("interrupt_with")
            second = (
                await self.synthesize(clarification, f"turn-{self.turn:02d}-interrupt")
                if clarification
                else None
            )
            after = await self.speak_file(text, path)
            if second:
                async with asyncio.timeout(40):
                    self.speech_activity.clear()
                    await self.speech_activity.wait()
                await asyncio.sleep(command.get("interrupt_after_s", 2))
                self.log(
                    "interruption.attempt",
                    speaker_active=time.monotonic() - self.last_active < 0.3,
                    speaker_capture_pts_us=self.last_audio_pts_us,
                )
                after = await self.speak_file(clarification, second)
            state = await self.wait_response(after, command.get("timeout", 60))
            return {"wait_result": state, **await self.snapshot()}
        raise ValueError(f"unknown command: {op}")


async def main(args):
    import sys

    repo = Path(__file__).resolve().parents[1]
    sid, token = new_id(), secrets.token_hex(32)
    artifact_root = repo / "artifacts"
    root = artifact_root / sid
    root.mkdir(parents=True, mode=0o700)
    harness = Harness(root)
    host = DockerHost(artifact_root, args.image, repo / "deploy/seccomp.json")
    report = {
        "session_id": sid,
        "url": args.url,
        "image": args.image,
        "harness": "external adaptive caller + macOS TTS",
        "artifact_dir": str(root),
    }
    try:
        worker = await host.launch(sid, None, token)
        report["image_id"] = worker["image_id"]
        async with Client(worker["url"], token) as client:
            async with asyncio.timeout(40):
                while True:
                    try:
                        await client.request("GET", "/health")
                        break
                    except httpx.HTTPError:
                        await asyncio.sleep(0.2)
            session = harness.session = await client.start_session(
                SessionConfig(
                    browser_control="native",
                    mode="audio",
                    camera=False,
                    recording=True,
                    persistent_events=True,
                    max_duration_s=1800,
                    permission_origins=["https://www.docket.io"],
                    capture_tail_s=2,
                )
            )
            async with session.connect_native() as native:
                harness.native = native
                await native.context.grant_permissions(["microphone"])
                harness.audio_task = asyncio.create_task(harness.capture())
                await native.page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
                print(json.dumps({"ready": True, **report, **await harness.snapshot()}), flush=True)
                while True:
                    line = await asyncio.to_thread(sys.stdin.readline)
                    if not line:
                        break
                    try:
                        command = json.loads(line)
                        harness.log("command", command=command)
                        if command["op"] == "stop":
                            break
                        result = await harness.command(command)
                        print(json.dumps({"result": result}), flush=True)
                    except Exception as exc:
                        harness.log("command.error", error=str(exc))
                        print(json.dumps({"error": str(exc)}), flush=True)
                await harness.snapshot()
                harness.audio_task.cancel()
                await asyncio.gather(harness.audio_task, return_exceptions=True)
            manifest = await session.inspect()
            report["state"] = manifest["state"]
            full = next(a for a in manifest["artifacts"] if a["kind"] == "video.recording")
            report["full_recording"] = full
            await session.download(full["path"], root / "conversation.webm")
            probe = await host.command(
                "exec",
                "mba-" + sid,
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name:format=duration",
                "-of",
                "json",
                "/artifacts/" + full["path"],
            )
            report["probe"] = json.loads(probe)
            await host.command(
                "exec",
                "mba-" + sid,
                "ffmpeg",
                "-v",
                "error",
                "-i",
                "/artifacts/" + full["path"],
                "-f",
                "null",
                "-",
            )
            report["recording_decoded"] = True
            events = []
            after = 0
            while batch := await session.event_page(after):
                events.extend(e.model_dump(mode="json") for e in batch)
                after = batch[-1].sequence
            (root / "events.json").write_text(json.dumps(events, indent=2))
            report["capture_gaps"] = [e for e in events if e["type"] == "capture.gap"]
            report["shutdown_events"] = [
                e for e in events if e["type"] == "browser.shutdown_forced"
            ]
            report["audio"] = {
                "chunks": harness.audio_chunks,
                "active_chunks": harness.audio_active_chunks,
                "peak": harness.audio_peak,
            }
            report["spoken_utterances"] = len(harness.transcript)
            report["artifact_count"] = len(manifest["artifacts"])
    except BaseException as exc:
        report["error"] = str(exc)
        raise
    finally:
        if harness.audio_task and not harness.audio_task.done():
            harness.audio_task.cancel()
            await asyncio.gather(harness.audio_task, return_exceptions=True)
        await host.remove(sid)
        report["worker_removed"] = await host.inspect("mba-" + sid) is None
        (root / "report.json").write_text(json.dumps(report, indent=2))
        print(json.dumps({"finished": report}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="mba-worker:harness-arm64")
    parser.add_argument("--url", default="https://www.docket.io/")
    asyncio.run(main(parser.parse_args()))
