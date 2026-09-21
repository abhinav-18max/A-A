import os
from pathlib import Path

import pytest

from mba import Adapter, AudioFormat, BindingConfig, SessionConfig, VideoFormat
from mba.evidence import Clock, Evidence
from mba.protocol import AdapterError


def test_direct_defaults_and_format_validation():
    config = SessionConfig()
    assert config.mode == "text"
    assert not config.camera_enabled
    assert config.binding.kind == "manual"
    assert config.recording and config.visual_enabled
    assert Adapter(config).artifact_dir == Path.cwd() / "artifacts"
    assert not SessionConfig(recording=False).visual_enabled
    assert not SessionConfig(visual=False).visual_enabled
    assert SessionConfig(mode="audio").visual_enabled
    assert AudioFormat(24000).wire().rate == 24000
    with pytest.raises(ValueError):
        AudioFormat(44100).wire()
    with pytest.raises(ValueError):
        VideoFormat(width=3).wire()


async def test_bounded_metadata_reports_expired_cursor(tmp_path):
    evidence = Evidence(tmp_path, "test", Clock(), persistent=False)
    for i in range(4100):
        await evidence.emit("value", {"i": i})
    assert len(evidence.read(0, 5000)) == 4096
    with pytest.raises(AdapterError, match="fell behind"):
        await anext(evidence.events(1))
    await evidence.close()
    evidence.db.close()
    assert not (tmp_path / "events.sqlite3").exists()


async def test_recording_default_retains_session_artifacts(tmp_path, monkeypatch, runtime_factory):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("mba.library.NativeRuntime", lambda **_: runtime_factory())
    async with Adapter() as session:
        root = session.core.evidence.root
        assert session.temporary is None
        assert root == tmp_path / "artifacts" / session.id
        assert session.core.config.persistent_events
    assert (root / "manifest.json").is_file()
    assert (root / "events.sqlite3").is_file()


async def test_media_interfaces_reject_crossed_modalities():
    from mba.library import Media

    with pytest.raises(ValueError, match="video input format"):
        Media(None, "video").open_input(AudioFormat())
    with pytest.raises(ValueError, match="audio input format"):
        Media(None, "audio").open_input(VideoFormat())
    with pytest.raises(AdapterError, match="visual.frames"):
        await anext(Media(None, "video").capture())


@pytest.mark.browser
@pytest.mark.skipif(os.environ.get("MBA_BROWSER_TESTS") != "1", reason="requires Chromium")
async def test_direct_typescript_browser(tmp_path):
    # A data URL avoids an external server; the worker must install observation before navigation.
    from urllib.parse import quote

    html = """<input id="composer"><button id="send">Send</button><div id="ready">Ready</div>
    <script>document.querySelector('#send').onclick=()=>{const c=document.querySelector('#composer');
    const d=document.createElement('div');d.className='assistant';d.textContent='draft';
    document.body.append(d);const v=c.value;c.value='';setTimeout(()=>d.textContent=v,20);}</script>"""
    # Navigate with the direct browser API; config validation keeps HTTP targets explicit.
    config = SessionConfig(recording=False, binding=BindingConfig(kind="manual"))
    before = dict(os.environ)
    async with Adapter(config, artifact_dir=tmp_path) as adapter:
        await adapter.browser.open("data:text/html," + quote(html))
        await adapter.browser.fill("#composer", "hello")
        await adapter.browser.click("#send")
        await adapter.browser.wait_for(".assistant")
        assert (await adapter.browser.screenshot()).read_bytes().startswith(b"\x89PNG")
        with pytest.raises(AdapterError, match="text binding"):
            action = await adapter.text.send("unbound")
            await action.wait()
    assert dict(os.environ) == before
    assert adapter.core.runtime.browser.child.process.returncode == 0
