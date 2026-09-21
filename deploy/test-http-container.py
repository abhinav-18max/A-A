"""Smoke test the production Docker entrypoint and optional remote Python SDK."""

import argparse
import asyncio
import secrets
from pathlib import Path
from urllib.parse import quote

import httpx

from mba import SessionConfig
from mba.controller import DockerHost
from mba.protocol import new_id
from mba.sdk import Client


async def main(image):
    repo = Path(__file__).resolve().parents[1]
    root = repo / ".cache/http-container-artifacts"
    root.mkdir(parents=True, exist_ok=True)
    host = DockerHost(root, image, repo / "deploy/seccomp.json")
    sid, token = new_id(), secrets.token_hex(32)
    try:
        worker = await host.launch(sid, None, token)
        async with Client(worker["url"], token) as client:
            async with asyncio.timeout(20):
                while True:
                    try:
                        await client.request("GET", "/health")
                        break
                    except httpx.HTTPError:
                        await asyncio.sleep(0.1)
            session = await client.start_session(SessionConfig())
            assert session.id == sid
            html = '<input id="text"><button onclick="document.body.dataset.sent=1">Send</button>'
            await session.browser_command("open", value="data:text/html," + quote(html))
            await session.browser_command("fill", "#text", "Remote container check")
            await session.browser_command("click", "button")
            await session.browser_command("wait", "body[data-sent='1']")
            frame = await session.checkpoint()
            destination = root / sid / "downloaded.png"
            await session.download(frame["path"], destination)
            assert destination.read_bytes().startswith(b"\x89PNG")
            events = await session.event_page()
            assert any(e.type == "target.ready" for e in events)
            manifest = await session.stop()
            assert manifest["state"] == "closed"
            full = next(a for a in manifest["artifacts"] if a["kind"] == "video.recording")
            assert full["audio_streams"] == []
            await session.download(full["path"], root / sid / "session.webm")
            probe = await host.command(
                "exec",
                "mba-" + sid,
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_name,codec_type",
                "-of",
                "json",
                "/artifacts/" + full["path"],
            )
            import json

            assert [s["codec_name"] for s in json.loads(probe)["streams"]] == ["vp8"]
        print(
            "PASS: production entrypoint, non-root UID, authenticated HTTP SDK, browser controls, artifact download, event replay, and shutdown"
        )
    finally:
        await host.remove(sid)
    assert await host.inspect(f"mba-{sid}") is None
    print("PASS: test worker container removed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="mba-worker:0.3.0-arm64")
    asyncio.run(main(parser.parse_args().image))
