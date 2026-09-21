"""Text-only sessions on a remote Chrome reached over CDP, such as a Browserbase session."""

import asyncio
import json
import os
import re
import tempfile
from contextlib import asynccontextmanager

import pytest
from pydantic import ValidationError

from mba import Adapter, RemoteBrowser, SessionConfig
from mba.browser_rpc import check_remote_host
from mba.protocol import AdapterError

REMOTE = RemoteBrowser(cdp_url="wss://connect.example.com/devtools?apiKey=secret")
BROWSER = pytest.mark.skipif(os.environ.get("MBA_BROWSER_TESTS") != "1", reason="requires Chromium")


def test_remote_browser_is_text_only_and_never_serialized():
    config = SessionConfig(recording=False, remote_browser=REMOTE)
    assert "remote_browser" not in config.model_dump(mode="json")
    assert "secret" not in json.dumps(config.request_body())
    for invalid in (
        {"mode": "audio"},
        {"recording": True},
        {"browser_control": "native"},
        {"visual": True},
    ):
        with pytest.raises(ValidationError, match="remote_browser"):
            SessionConfig(**{"recording": False, "remote_browser": REMOTE, **invalid})


def test_remote_browser_is_rejected_by_http_and_allowlisted_locally(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from mba.service import create_worker_app

    token = "integration-test-token-with-enough-length"
    with TestClient(create_worker_app(tmp_path, token)) as client:
        response = client.post(
            "/v1/sessions",
            headers={"Authorization": "Bearer " + token},
            json={
                "mode": "text",
                "recording": False,
                "binding": {"kind": "manual"},
                "remote_browser": REMOTE.model_dump(),
            },
        )
    assert response.status_code == 422 and response.json()["error"] == "remote_browser_library_only"

    monkeypatch.delenv("MBA_REMOTE_CDP_HOSTS", raising=False)
    with pytest.raises(AdapterError, match="MBA_REMOTE_CDP_HOSTS"):
        check_remote_host(REMOTE.cdp_url)
    monkeypatch.setenv("MBA_REMOTE_CDP_HOSTS", "*.example.com, 127.0.0.1")
    check_remote_host(REMOTE.cdp_url)
    check_remote_host("ws://127.0.0.1:9222/devtools/browser/x")
    with pytest.raises(AdapterError):
        check_remote_host("wss://example.com.attacker.net/devtools")


@asynccontextmanager
async def remote_chrome():
    """A separately launched Chrome with remote debugging stands in for a hosted browser."""
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        executable = playwright.chromium.executable_path
    with tempfile.TemporaryDirectory() as profile:
        process = await asyncio.create_subprocess_exec(
            executable,
            "--headless=new",
            "--remote-debugging-port=0",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(30):
                while True:
                    line = (await process.stderr.readline()).decode()
                    if match := re.search(r"DevTools listening on (ws://\S+)", line):
                        break
            yield match.group(1)
        finally:
            process.kill()
            await process.wait()


@asynccontextmanager
async def form_site():
    body = (
        b'<input id="composer"><button id="send" onclick="const d=document.createElement(\'p\');'
        b"d.textContent=document.querySelector('#composer').value;document.body.append(d)\">"
        b"Send</button>"
    )

    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.browser
@BROWSER
async def test_text_session_on_remote_cdp_browser(tmp_path, monkeypatch):
    async with remote_chrome() as cdp, form_site() as url:
        config = SessionConfig(
            recording=False, remote_browser=RemoteBrowser(cdp_url=cdp), target_url=url
        )
        monkeypatch.delenv("MBA_REMOTE_CDP_HOSTS", raising=False)
        with pytest.raises(AdapterError) as error:
            async with Adapter(config, artifact_dir=tmp_path):
                pass
        assert error.value.code == "remote_browser_not_allowed"

        monkeypatch.setenv("MBA_REMOTE_CDP_HOSTS", "127.0.0.1")
        async with Adapter(config, artifact_dir=tmp_path) as session:
            capabilities = await session.capabilities()
            assert capabilities["remote_browser"] and not capabilities["audio_input"]
            await session.browser.fill("#composer", "remote hello")
            await session.browser.click("#send")
            snapshot = await session.browser.snapshot()
            assert "remote hello" in snapshot["accessibility"]
            assert (await session.visual.screenshot()).is_file()
            ready = next(e for e in session.core.evidence.read(0) if e.type == "target.ready")
            assert ready.data["remote_browser"]
            assert cdp not in json.dumps(await session.inspect())
