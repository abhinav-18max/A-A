"""Real Chromium integration through the TypeScript worker and public Python API."""

import asyncio
import os
from contextlib import asynccontextmanager

import pytest

from mba import Adapter, BindingConfig, SessionConfig
from mba.generated import adapter_pb2 as pb
from mba.protocol import AdapterError

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(os.environ.get("MBA_BROWSER_TESTS") != "1", reason="requires Chromium"),
]


@asynccontextmanager
async def website():
    pages = {
        "/target": '<iframe id="chat" src="/frame"></iframe>',
        "/frame": """<div id="ready">Ready</div><input id="composer"><button id="send">Send</button>
        <script>document.querySelector('#send').onclick=()=>{const i=document.querySelector('#composer');
        const v=i.value;i.value='';const n=document.createElement('div');n.className='assistant';
        n.textContent='draft';document.body.append(n);setTimeout(()=>n.textContent=v,20);}</script>""",
    }

    async def handle(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        path = request.split(b" ")[1].decode()
        body = pages.get(path, pages["/frame"]).encode()
        writer.write(
            f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    finally:
        server.close()
        await server.wait_closed()


async def test_iframe_revisions_and_navigation(tmp_path):
    async with website() as url:
        config = SessionConfig(
            recording=False,
            target_url=url + "/target",
            binding=BindingConfig(
                kind="declarative", frame_selector="#chat", message_selector=".assistant"
            ),
        )
        async with Adapter(config, artifact_dir=tmp_path) as session:
            await (await session.text.send("first response")).wait()
            await asyncio.sleep(0.15)
            texts = [
                e.data["content"]
                for e in session.core.evidence.read(0)
                if e.type == "text.revision"
            ]
            assert texts == ["draft", "first response"]
            await session.browser.open(url + "/target")
            await (await session.text.send("second response")).wait()
            await asyncio.sleep(0.15)
            events = [e for e in session.core.evidence.read(0) if e.type == "text.revision"]
            assert [e.data["content"] for e in events] == [
                "draft",
                "first response",
                "draft",
                "second response",
            ]
            assert events[0].data["message_id"] != events[2].data["message_id"]
            assert (await session.visual.screenshot()).is_file()


async def test_two_direct_sessions_and_terminal_browser_loss(tmp_path):
    before = dict(os.environ)
    async with (
        Adapter(SessionConfig(recording=False), artifact_dir=tmp_path) as a,
        Adapter(SessionConfig(recording=False), artifact_dir=tmp_path) as b,
    ):
        assert a.id != b.id
        assert a.core.runtime.socket_dir.name != b.core.runtime.socket_dir.name
        assert a.core.runtime.media is None and b.core.runtime.media is None
        with pytest.raises(Exception, match="protocol version"):
            await a.core.runtime.browser.stub.Start(pb.BrowserStart(protocol_version=99))
        a.core.runtime.browser.child.process.kill()
        await a.core.runtime.browser.child.process.wait()
        async with asyncio.timeout(5):
            async for event in a.events():
                if event.type == "session.state" and event.data["state"] == "failed":
                    break
        assert b.core.state == "ready"
        await b.browser.open("data:text/html,<p>still alive</p>")
        assert (await b.browser.screenshot()).is_file()
    assert dict(os.environ) == before


async def test_failed_start_releases_child_process(tmp_path):
    async with website() as url:
        adapter = Adapter(
            SessionConfig(
                recording=False,
                target_url=url,
                startup_timeout_s=2,
                binding=BindingConfig(kind="declarative", ready_selector="#missing"),
            ),
            artifact_dir=tmp_path,
        )
        with pytest.raises((TimeoutError, AdapterError)):
            await adapter.__aenter__()
        assert adapter.core.runtime.browser.child.process.returncode is not None
        assert not os.path.exists(adapter.core.runtime.socket_dir.name)
