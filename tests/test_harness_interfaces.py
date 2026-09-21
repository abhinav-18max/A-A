import asyncio
import os
from pathlib import Path

import pytest

from mba import Adapter, SessionConfig
from mba.protocol import AdapterError, StreamConfig
from mba.streams import HEADER, InputStream


@pytest.mark.parametrize("rate,channels", [(16000, 1), (24000, 2), (48000, 1)])
async def test_negotiated_audio_frames(rate, channels):
    config = StreamConfig(kind="audio", format="S16LE", rate=rate, channels=channels)
    stream = InputStream(config)
    await stream.put(HEADER.pack(0, 0) + bytes(rate // 50 * channels * 2))
    with pytest.raises(AdapterError, match="frame payload"):
        await stream.put(HEADER.pack(1, 20000) + b"bad")
    await stream.end()
    assert len([p async for p in stream.packets()]) == 1


async def test_negotiated_video_rate():
    stream = InputStream(StreamConfig(kind="video", format="RGB24", width=4, height=2, fps=60))
    await stream.put(HEADER.pack(0, 0) + bytes(24))
    await stream.put(HEADER.pack(1, 16666) + bytes(24))
    with pytest.raises(AdapterError, match="declared FPS"):
        await stream.put(HEADER.pack(2, 66666) + bytes(24))


BROWSER = pytest.mark.skipif(os.environ.get("MBA_BROWSER_TESTS") != "1", reason="requires Chromium")
HTML = """<input id="composer" data-bind="semi;colon"><button id="send" onclick="const x=document.createElement('div');x.className='assistant';x.textContent=document.querySelector('input').value;document.body.append(x);document.querySelector('input').value=''">Send</button><input type="checkbox" id="check"><select><option value="one">One</option><option value="two">Two</option></select><div id="double" ondblclick="this.textContent='done'">double</div><iframe srcdoc="<button>Frame</button>"></iframe>"""


@pytest.mark.browser
@BROWSER
async def test_tools_page_registry_bindings_and_document_guard(tmp_path):
    from urllib.parse import quote

    async with Adapter(SessionConfig(recording=False), artifact_dir=tmp_path) as session:
        await session.browser.open("data:text/html," + quote(HTML))
        snapshot = await session.browser.snapshot()
        pid = snapshot["page_id"]
        assert "Send" in snapshot["accessibility"] and len(snapshot["frames"]) == 2
        await session.browser.command(
            "fill",
            value="dynamic binding",
            options={"target": {"kind": "css", "value": "#composer"}},
        )
        await session.browser.command(
            "click", options={"target": {"kind": "role", "role": "button", "name": "Send"}}
        )
        await session.text.bind(
            {
                "kind": "manual",
                "input_selector": '[data-bind="semi;colon"]',
                "submit_selector": "#send",
                "message_selector": ".assistant",
            }
        )
        await (await session.text.send("second")).wait()
        await session.browser.double_click("#double")
        await session.browser.command("check", "#check")
        await session.browser.command("select", "select", "two")
        second = await session.browser.new_page("data:text/html,second")
        assert len((await session.browser.pages())["pages"]) == 2
        await session.browser.select_page(pid)
        await session.browser.open("data:text/html,new document")
        with pytest.raises(AdapterError) as exc:
            await session.browser.command(
                "click", "#send", expected_document_id=snapshot["document_id"]
            )
        assert exc.value.code == "stale_document"
        await session.browser.close_page(second["page_id"])
        assert len((await session.browser.pages())["pages"]) == 1


@pytest.mark.browser
@BROWSER
async def test_native_python_objects_observation_and_ownership(tmp_path):
    from playwright.async_api import Page
    from websockets.asyncio.client import connect
    from websockets.exceptions import InvalidStatus

    async with Adapter(
        SessionConfig(recording=False, browser_control="native"), artifact_dir=tmp_path
    ) as session:
        descriptor = await session.native_descriptor()
        with pytest.raises(InvalidStatus):
            async with connect(descriptor["endpoint"]):
                pass
        async with session.connect_native() as native:
            assert isinstance(native.page, Page)
            assert native.browser.version
            await native.page.set_content(HTML)
            await native.bind_text(
                native.page, input_selector='[data-bind="semi;colon"]', message_selector=".old"
            )
            await native.bind_text(native.page, message_selector=".assistant")
            # Rebinding must survive navigation even if init scripts run out of order.
            from urllib.parse import quote

            await native.page.goto("data:text/html," + quote(HTML))
            await native.page.locator("#composer").fill("hello native")
            await native.page.get_by_role("button", name="Send", exact=True).click()
            await native.page.frame_locator("iframe").get_by_role("button").click()
            await native.page.route("**/fixture", lambda route: route.fulfill(body="intercepted"))
            popup = await native.context.new_page()
            await popup.set_content("<p>popup</p>")
            await native.select_page(popup)
            await native.select_page(native.context.pages[0])
            with pytest.raises(InvalidStatus):
                async with connect(
                    descriptor["endpoint"], additional_headers=descriptor["headers"]
                ):
                    pass
            assert await native.page.locator(".assistant").inner_text() == "hello native"
            with pytest.raises(AdapterError) as exc:
                await session.browser.click("#send")
            assert exc.value.code == "browser_control_external"
            with pytest.raises(AdapterError) as exc:
                await session.text.send("forbidden")
            assert exc.value.code == "browser_control_external"
            async with asyncio.timeout(5):
                async for event in session.events():
                    if event.type == "text.revision" and event.data["content"] == "hello native":
                        assert event.data["provenance"] == "native_helper"
                        break
        assert (await session.inspect())["state"] == "closed"


@pytest.mark.browser
@BROWSER
async def test_native_disconnect_stops_session(tmp_path):
    from playwright.async_api import async_playwright

    async with Adapter(
        SessionConfig(recording=False, browser_control="native"), artifact_dir=tmp_path
    ) as session:
        descriptor = await session.native_descriptor()
        async with async_playwright() as pw:
            browser = await pw.chromium.connect(
                descriptor["endpoint"], headers=descriptor["headers"]
            )
            await browser.new_context()
            await browser.close()
        async with asyncio.timeout(8):
            async for event in session.events():
                if event.type == "session.state" and event.data["state"] in {"closed", "failed"}:
                    break
        assert (await session.inspect())["error"] == "native_client_disconnected"


@pytest.mark.browser
@BROWSER
async def test_mcp_stdio_real_client():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=str(Path(".venv/bin/python").absolute()),
        args=["-m", "mba.cli", "mcp"],
        env=dict(os.environ),
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
        await client.initialize()
        assert any(t.name == "aa_media_open" for t in (await client.list_tools()).tools)
        await client.list_tools()  # schema definitions must survive repeated discovery
        result = await client.call_tool(
            "aa_session_create", {"config": {"mode": "text", "recording": False}}
        )
        assert not result.isError, result
        sid = result.structuredContent["session_id"]
        result = await client.call_tool(
            "aa_browser",
            {
                "session_id": sid,
                "command": {"operation": "open", "value": "data:text/html,<button>Hello</button>"},
            },
        )
        assert not result.isError, result
        shot = await client.call_tool("aa_screenshot", {"session_id": sid})
        assert not shot.isError and any(c.type == "image" for c in shot.content)
        bad = await client.call_tool(
            "aa_browser", {"session_id": sid, "command": {"operation": "invalid"}}
        )
        assert bad.isError
        closed = await client.call_tool("aa_session_close", {"session_id": sid})
        assert closed.structuredContent["state"] == "closed"


@pytest.mark.browser
@BROWSER
async def test_remote_python_native(worker_endpoint, tmp_path):
    from urllib.parse import quote

    from mba.sdk import Client

    url, token = worker_endpoint
    async with Client(url, token) as client:
        session = await client.start_session(
            SessionConfig(recording=False, browser_control="native")
        )
        async with session.connect_native() as native:
            await native.page.goto("data:text/html," + quote(HTML))
            await native.bind_text(native.page, message_selector=".assistant")
            await native.page.get_by_role("textbox").fill("remote native")
            await native.page.get_by_role("button", name="Send", exact=True).click()
            assert (await session.capabilities())["native_connected"]
            async with asyncio.timeout(5):
                async for event in session.events():
                    if event.type == "text.revision" and event.data["content"] == "remote native":
                        break
        assert (await session.inspect())["state"] == "closed"


@pytest.mark.browser
@BROWSER
async def test_typescript_native_client(worker_endpoint):
    url, token = worker_endpoint
    process = await asyncio.create_subprocess_exec(
        "node",
        "tests/native-client.mjs",
        cwd="harness-client",
        env={**os.environ, "MBA_URL": url, "MBA_TOKEN": token},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 35)
        assert process.returncode == 0, stderr.decode()
        assert "PASS" in stdout.decode()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
