"""WebMCP tools registered by pages, exercised through real Chromium and the worker."""

import asyncio
import os
from contextlib import asynccontextmanager

import pytest

from mba import Adapter, SessionConfig
from mba.protocol import AdapterError

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(os.environ.get("MBA_BROWSER_TESTS") != "1", reason="requires Chromium"),
]


def register(name, body):
    return (
        f"<script>document.modelContext?.registerTool({{name:'{name}',description:'{name} tool',"
        "inputSchema:{type:'object',properties:{a:{type:'number'},b:{type:'number'},q:{type:'string'}}},"
        f"execute:async(args)=>{{{body}}}}});</script>"
    )


PAGES = {
    "/tools": register(
        "add_numbers", "return {content:[{type:'text',text:String(args.a+args.b)}]};"
    )
    + register("never", "return new Promise(()=>{});")
    + register("big", "return {content:[{type:'text',text:'x'.repeat(300*1024)}]};")
    + register("reload", "setTimeout(()=>location.reload(),0);return new Promise(()=>{});")
    + "<button id='self' onclick=\"document.modelContext.getTools().then(t=>"
    "document.modelContext.executeTool(t.find(x=>x.name==='add_numbers'),"
    'JSON.stringify({a:1,b:1})))">Self</button>',
    "/plain": "<p>No tools here</p>",
    "/widget": register("echo", "return 'widget:'+args.q;")
    + register("widget_only", "return 'only:'+args.q;"),
    "/host-allowed": register("echo", "return 'host:'+args.q;")
    + '<iframe allow="tools" src="http://localhost:{port}/widget"></iframe>',
    "/host-blocked": '<iframe src="http://localhost:{port}/widget"></iframe>',
}


@asynccontextmanager
async def website():
    port = 0

    async def handle(reader, writer):
        request = await reader.readuntil(b"\r\n\r\n")
        path = request.split(b" ")[1].decode()
        body = PAGES.get(path, PAGES["/plain"]).replace("{port}", str(port)).encode()
        writer.write(
            f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        # 127.0.0.1 and localhost are distinct secure-context origins, as WebMCP requires.
        yield f"http://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


async def tools(session, names):
    async with asyncio.timeout(5):
        while True:
            listing = await session.webmcp.tools()
            if names <= {t["name"] for t in listing["tools"]}:
                return listing
            await asyncio.sleep(0.05)


async def test_disabled_by_default(tmp_path):
    async with Adapter(SessionConfig(recording=False), artifact_dir=tmp_path) as session:
        capabilities = await session.capabilities()
        assert not capabilities["webmcp"]["enabled"]
        with pytest.raises(AdapterError) as error:
            await session.webmcp.tools()
        assert error.value.code == "webmcp_disabled"


async def test_list_call_limits_and_journal(tmp_path):
    async with website() as url:
        config = SessionConfig(recording=False, webmcp=True, target_url=url + "/tools")
        async with Adapter(config, artifact_dir=tmp_path) as session:
            capabilities = await session.capabilities()
            assert capabilities["webmcp"]["enabled"]
            assert {"webmcp_tools", "webmcp_call"} <= set(capabilities["operations"])
            listing = await tools(session, {"add_numbers", "never", "big", "reload"})
            assert listing["available"] and listing["blocked_frames"] == []
            tool = next(t for t in listing["tools"] if t["name"] == "add_numbers")
            assert tool["tool_source"] == "site" and tool["input_schema"]["type"] == "object"
            assert tool["page_id"] and tool["frame_id"] and tool["document_id"]

            result = await session.webmcp.call("add_numbers", {"a": 2, "b": 3})
            assert result["status"] == "completed" and result["untrusted"]
            assert result["result"]["content"][0]["text"] == "5"
            assert result["output_sha256"] and not result["truncated"]

            with pytest.raises(AdapterError) as error:
                await session.webmcp.call("never", timeout_ms=500)
            assert error.value.code == "webmcp_timeout"
            # A tool that never answers must not block later mutating commands.
            assert (await session.webmcp.call("add_numbers", {"a": 1, "b": 2}))["result"]
            await session.browser.click("#self")

            big = await session.webmcp.call("big")
            assert big["truncated"] and big["result"] is None
            assert big["output_bytes"] > len(big["output_text"])
            with pytest.raises(AdapterError) as error:
                await session.webmcp.call("missing")
            assert error.value.code == "webmcp_tool_not_found"
            assert (await session.webmcp.call("reload"))["status"] == "navigated"

            async with asyncio.timeout(5):
                while True:
                    events = session.core.evidence.read(0)
                    invoked = [e for e in events if e.type == "webmcp.invoked"]
                    if {e.data["initiator"] for e in invoked} >= {"adapter", "page"}:
                        break
                    await asyncio.sleep(0.05)
            added = [e for e in events if e.type == "webmcp.tools_changed"]
            assert any(t["name"] == "add_numbers" for e in added for t in e.data["tools"])
            responded = [e for e in events if e.type == "webmcp.responded"]
            assert any(e.data["status"] == "Completed" for e in responded)
            assert all(e.observed_at_us > 0 for e in invoked + responded)


async def test_frames_ambiguity_and_permissions_policy(tmp_path):
    async with website() as url:
        config = SessionConfig(recording=False, webmcp=True, target_url=url + "/host-allowed")
        async with Adapter(config, artifact_dir=tmp_path) as session:
            listing = await tools(session, {"echo", "widget_only"})
            echoes = [t for t in listing["tools"] if t["name"] == "echo"]
            assert len(echoes) == 2
            with pytest.raises(AdapterError) as error:
                await session.webmcp.call("echo", {"q": "x"})
            assert error.value.code == "webmcp_ambiguous_tool"
            widget = next(t for t in echoes if "localhost" in t["frame_url"])
            result = await session.webmcp.call("echo", {"q": "x"}, frame_id=widget["frame_id"])
            assert result["result"] == "widget:x"
            assert (await session.webmcp.call("widget_only", {"q": "y"}))["result"] == "only:y"

            await session.browser.open(url + "/host-blocked")
            async with asyncio.timeout(5):
                while True:
                    listing = await session.webmcp.tools()
                    if listing["blocked_frames"]:
                        break
                    await asyncio.sleep(0.05)
            assert listing["blocked_frames"][0]["reason"] == "permissions_policy"
            assert listing["tools"] == []

            await session.browser.open(url + "/plain")
            assert (await session.webmcp.tools())["tools"] == []


FORM = (
    '<input id="composer"><button id="send" onclick="const d=document.createElement(\'p\');'
    "d.className='assistant';d.textContent=document.querySelector('#composer').value;"
    'document.body.append(d)">Send</button>'
)
PAGES["/form"] = FORM


def adapter(url):
    from mba import AdapterStep, AdapterTool, SiteAdapter

    return SiteAdapter(
        name="fixture",
        url_pattern=url + "/form*",
        tools=[
            AdapterTool(
                name="say",
                description="Type a message and send it",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                steps=[
                    AdapterStep(operation="fill", selector="#composer", value="{{text}}"),
                    AdapterStep(operation="click", selector="#send"),
                    AdapterStep(operation="wait", selector=".assistant"),
                    AdapterStep(operation="snapshot"),
                ],
            )
        ],
    )


async def test_site_adapter_on_a_page_without_webmcp(tmp_path):
    async with website() as url:
        config = SessionConfig(recording=False, target_url=url + "/form")
        async with Adapter(config, artifact_dir=tmp_path) as session:
            registered = await session.webmcp.add_adapter(adapter(url))
            assert registered["tools"] == ["say"]
            listing = await session.webmcp.tools()
            assert not listing["site_enabled"]
            (tool,) = listing["tools"]
            assert tool["tool_source"] == "tester" and tool["adapter"] == "fixture"
            result = await session.webmcp.call("say", {"text": "hello adapter"})
            assert result["tool_source"] == "tester" and result["status"] == "completed"
            assert "hello adapter" in result["result"]["steps"][3]["result"]["accessibility"]
            with pytest.raises(AdapterError) as error:
                await session.webmcp.call("say", {})
            assert error.value.code == "webmcp_missing_argument"
            with pytest.raises(AdapterError) as error:
                await session.webmcp.call("say", {"text": "x"}, source="site")
            assert error.value.code == "webmcp_disabled"
            events = [e for e in session.core.evidence.read(0) if e.type.startswith("webmcp.")]
            assert [e.data["tool_source"] for e in events[:2]] == ["tester", "tester"]
            assert events[1].data["status"] == "Completed"

            await session.browser.open(url + "/plain")
            with pytest.raises(AdapterError) as error:
                await session.webmcp.tools()  # URL pattern no longer matches
            assert error.value.code == "webmcp_disabled"
            assert (await session.webmcp.remove_adapter("fixture"))["adapters"] == []


async def test_remote_sdk_mcp_and_native(worker_endpoint, tmp_path):
    from pathlib import Path

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from mba.sdk import Client

    async with website() as url:
        endpoint, token = worker_endpoint
        async with Client(endpoint, token) as client:
            config = SessionConfig(recording=False, webmcp=True, target_url=url + "/tools")
            session = await client.start_session(config)
            try:
                assert "add_numbers" in {t["name"] for t in (await tools(session, set()))["tools"]}
                result = await session.webmcp.call("add_numbers", {"a": 20, "b": 22})
                assert result["result"]["content"][0]["text"] == "42"
            finally:
                await session.stop()
            # Plain sessions omit the field, so older workers still accept them.
            assert "webmcp" not in SessionConfig(recording=False).request_body()

        params = StdioServerParameters(
            command=str(Path(".venv/bin/python").absolute()),
            args=["-m", "mba.cli", "mcp"],
            env=dict(os.environ),
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as mcp:
            await mcp.initialize()
            names = {t.name for t in (await mcp.list_tools()).tools}
            assert {"aa_webmcp_tools", "aa_webmcp_call", "aa_webmcp_adapter"} <= names
            created = await mcp.call_tool(
                "aa_session_create",
                {"config": {"recording": False, "webmcp": True, "target_url": url + "/tools"}},
            )
            sid = created.structuredContent["session_id"]
            async with asyncio.timeout(5):
                while True:
                    listed = await mcp.call_tool("aa_webmcp_tools", {"session_id": sid})
                    if listed.structuredContent["tools"]:
                        break
                    await asyncio.sleep(0.05)
            called = await mcp.call_tool(
                "aa_webmcp_call",
                {"session_id": sid, "name": "add_numbers", "arguments": {"a": 1, "b": 1}},
            )
            assert not called.isError and called.structuredContent["untrusted"]
            await mcp.call_tool("aa_session_close", {"session_id": sid})

        native_config = SessionConfig(recording=False, browser_control="native", webmcp=True)
        async with Adapter(native_config, artifact_dir=tmp_path) as session:
            async with session.connect_native() as native:
                await native.page.goto(url + "/tools")
                async with asyncio.timeout(5):
                    while True:
                        if (await native.webmcp_tools(native.page))["tools"]:
                            break
                        await asyncio.sleep(0.05)
                result = await native.webmcp_call(native.page, "add_numbers", {"a": 3, "b": 4})
                assert result["result"]["content"][0]["text"] == "7"
                async with asyncio.timeout(5):
                    async for event in session.events():
                        if event.type == "webmcp.responded":
                            assert event.data["provenance"] == "native_helper"
                            break


async def test_typescript_client_media_framing_and_webmcp(worker_endpoint, tmp_path):
    import json

    from mba.generated import adapter_pb2 as pb

    endpoint, token = worker_endpoint  # A worker accepts one session; this test needs its own.
    async with website() as url:
        packet = pb.MediaPacket(
            stream_id="speaker",
            sequence=300,
            pts_us=6_000_000_123,
            data=bytes(range(256)) * 4,
            format=pb.MediaFormat(kind="audio", encoding="S16LE", rate=48000, channels=2),
            discontinuity=True,
        )
        (tmp_path / "packet.bin").write_bytes(packet.SerializeToString())
        (tmp_path / "upload.txt").write_text("asset payload")
        process = await asyncio.create_subprocess_exec(
            "node",
            "tests/media-client.mjs",
            cwd="harness-client",
            env={
                **os.environ,
                "MBA_URL": endpoint,
                "MBA_TOKEN": token,
                "TARGET_URL": url + "/tools",
                "FORM_URL": url + "/form",
                "PACKET": str(tmp_path / "packet.bin"),
                "UPLOAD": str(tmp_path / "upload.txt"),
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
        assert process.returncode == 0, stderr.decode()
        report = json.loads(stdout.decode().splitlines()[-1])
        assert report["packet"] == {
            "streamId": "speaker",
            "sequence": 300,
            "ptsUs": 6_000_000_123,
            "bytes": 1024,
            "format": {"kind": "audio", "encoding": "S16LE", "rate": 48000, "channels": 2},
            "discontinuity": True,
        }
        assert report["sum"] == "42" and report["tester"] == "completed"
        assert report["asset"] and report["event"] == "webmcp.invoked"


async def test_typescript_native_webmcp(worker_endpoint):
    endpoint, token = worker_endpoint
    async with website() as url:
        process = await asyncio.create_subprocess_exec(
            "node",
            "tests/native-webmcp.mjs",
            cwd="harness-client",
            env={
                **os.environ,
                "MBA_URL": endpoint,
                "MBA_TOKEN": token,
                "TARGET_URL": url + "/tools",
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
        assert process.returncode == 0, stderr.decode()
        assert "PASS" in stdout.decode()
