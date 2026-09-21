# A&A — multimodal agent-to-agent adapter

A Python library for interacting with a browser-based agent through text, microphone
input, camera input, browser audio output, and rendered visuals. Functions, scripts,
and realtime connectors use the same session API. Scenarios and evaluation belong to
the calling application.

```text
Function / script / realtime connector
                  ↕
          Python library (mba)
             ↙           ↘
 TypeScript / Playwright   Rust / GStreamer
             ↘           ↙
                Chromium
                    ↕
              Target website
```

Browser execution lives in `browser-worker/`; media execution lives in `media-core/`.
Python coordinates these child processes over private Unix sockets using generated
gRPC/Protobuf contracts. Direct use does not require Docker, an HTTP server, or NATS.

**Verification:** real Chromium tests exercise the TypeScript worker; Rust tests exercise
GStreamer decoding/resampling, RGB camera-frame conversion, and WAV recordings.
Linux audio/display routing and container execution are verified on Docker Desktop ARM64.
Camera routing and latency/soak release gates still require a prepared Linux host.
See [validation](docs/validation.md).

## Harness interfaces

Choose `browser_control="tools"` (default) for SDK/MCP browser operations or
`browser_control="native"` for external Python/TypeScript Playwright clients. Native
helpers return ordinary Playwright objects and keep media on the same A&A session.
The optional `mba mcp` command exposes stdio tools outside the worker.

See [harness integration](docs/harness-integration.md) for installation, native client
examples, ownership, remote streaming, and compatibility limits. The TypeScript
`@mba/harness-client` has the same audio, camera, visual, recording and event methods as
the Python SDK. To host workers yourself, follow the [cloud runbook](docs/deploy-cloud.md).

## Install and use text mode

Requires Python 3.12–3.13 and Node.js 22+. From a checkout:

```sh
uv sync --locked --extra dev
npm ci --prefix browser-worker
uv run python tools/browser_build.py
uv run mba setup --browser
```

For a built wheel, install it and run `mba setup --browser`. Setup explicitly installs
locked Node dependencies and Chromium into `~/.cache/mba/0.3.0`. Nothing is downloaded
when importing the library or starting a session. Override the location with
`MBA_RUNTIME_DIR`; use the same location when running setup.

Sessions now record their full rendered display by default. Run them on the Linux
worker, including when the interaction itself is text-only. For lightweight local
Mac text/browser use, explicitly select `SessionConfig(recording=False)`.

```python
import asyncio
from mba import Adapter, BindingConfig, SessionConfig

async def main():
    config = SessionConfig(
        recording=False,  # Opt out for this local, browser-only example.
        target_url="https://your-target.example/chat",
        binding=BindingConfig(
            kind="declarative",
            ready_selector="textarea.message",
            input_selector="textarea.message",
            submit_selector="button.send",
            message_selector=".message.assistant",
        ),
    )
    async with Adapter(config, artifact_dir="artifacts") as session:
        action = await session.text.send("Hello")
        await action.wait()
        async for event in session.text.observations():
            print(event.data)
            break

asyncio.run(main())
```

Selectors above are illustrative. Configure the real target's controls. A manual
session does not need a chat binding:

```python
async with Adapter() as session:
    await session.browser.open("https://your-target.example")
    await session.browser.click("button.start")
    await session.browser.fill("textarea", "Hello")
    await session.browser.press("Enter", selector="textarea")
    image = await session.visual.screenshot()
    print(image.read_bytes()[:8])
```

Other browser operations: `wait_for(selector)`, `screenshot()`, and optional
`frame_selector`/`timeout_ms` on selector operations. Arbitrary Playwright objects are
not passed across processes. Browser loss fails the session; actions are not replayed.

## Audio and visuals on Linux

Full media execution requires Ubuntu 24.04 x86-64 with GStreamer plugins, PulseAudio,
Xvfb/Openbox, and the `mba-media` binary. A camera is required only for camera input.
Audio-only and rendered-display capture do not require v4l2loopback.

```sh
cargo build --release --locked --manifest-path media-core/Cargo.toml
export MBA_MEDIA_BINARY="$PWD/media-core/target/release/mba-media"
```

`deploy/Dockerfile` lists the native dependencies and builds an optional remote worker.
`deploy/provision-host.sh` explicitly provisions host cameras; it is not run by the
library. Host kernel modules are not installed during session startup.

Modes:

| Mode | Default capabilities |
|---|---|
| `text` | Browser actions, text observations, screenshots |
| `audio` | Text plus microphone playback and speaker capture |
| `video` | Text plus camera input and rendered-display capture |
| `multimodal` | All of the above |

Use `camera=False` or `visual=False` to disable those devices independently. For
rendered visuals without a camera use `mode="video", camera=False`. Text mode is
headless by default; media sessions use headed Chromium on a private display.

```python
from mba import Adapter, AudioFormat, SessionConfig

async with Adapter(
    SessionConfig(mode="audio", target_url="https://your-target.example"),
    artifact_dir="artifacts",
) as session:
    await session.browser.click("button.start-call")
    await (await session.audio.play("question.wav")).wait()
    async for chunk in session.audio.capture(channels=1):
        await consume_audio(chunk)
        break

    # Each write is 20 ms of S16LE PCM: 960 bytes at 24 kHz mono.
    async with session.audio.open_input(AudioFormat(rate=24000)) as stream:
        async for pcm in your_audio_source():
            await stream.write(pcm)
```

The audio input accepts S16LE PCM at 16/24/48 kHz, mono or stereo; Rust normalizes it to
48 kHz mono for the microphone. Capture defaults to 48 kHz stereo; request mono with
`channels=1`. Simultaneous send/capture should run in separate asyncio tasks.

For camera input use `session.camera.play(path)` or
`session.camera.open_input(VideoFormat(...))`. RGB24 and YUY2 frames are normalized to
1280×720 YUY2 at 30 FPS. RGB widths must be divisible by four; YUY2 widths must be even.
Maximum input geometry is 1920×1080 at 60 FPS. Video assets do not implicitly play their
audio; submit an audio track explicitly when needed.

`session.visual.frames(fps=1)` yields rendered RGB frames; use up to 30 FPS. This is
browser/display capture, not interception of the target's underlying video stream.
Camera injection is separate from browser screen sharing, which is not implemented.

Input streams are single-use. Normal context exit sends EOS and drains the action;
`cancel()` stops it. Timestamps on captured chunks/frames are session-relative. Input
writes are paced from zero at the declared rate; caller chunk timestamps are not used
as an absolute playback schedule. Use `session.submit(Action(...))` for synchronized
asset bundles with a common start time and offsets.

## Realtime connectors and callbacks

`mba.connectors` provides `Connector`, `Capabilities`, `Routes`, `Output`, and `Bridge`.
No provider SDK or model credentials are required by the adapter.

```python
from mba.connectors import Bridge, Routes

async with Bridge(
    session,
    your_connector,
    Routes(target_audio=True, output_audio=True),
) as bridge:
    await bridge.wait()
```

Routes are explicit. Text input forwards DOM observations including revisions; the
connector chooses how to interpret them. Text output means a complete submission.
Audio/video output carries a `response_id`; an `end` output drains its input streams.
`bridge.interrupt(response_id)` invalidates local playback before calling the
connector's interrupt method. Late outputs for that response are ignored.

The bridge does not infer turns, evaluate responses, or decide when to interrupt.
`examples/connector_fixture.py` demonstrates a credential-free connector contract.
Native connector implementations can obtain the session-scoped gRPC socket and clock
mapping through `session.media_endpoint()`; they must still prepare actions through
the coordinator before feeding streams and must not bypass action ownership.

For bounded callbacks:

```python
async with session.subscribe(your_async_callback, capacity=64) as subscription:
    await subscription.wait()
```

Errors and overload terminate that subscription and are surfaced to the caller;
callback failures also emit `extension.failed`. They do not run on capture threads.

## Events, recordings, and lifecycle

Full-session recording is **on by default**, and automatically enables rendered
display capture unless `visual=False` is explicitly requested. On session close,
`recordings/session-<timestamp>.webm` contains the screen (VP8, 1280×720, 30 FPS) and,
for audio-enabled sessions, one Opus track mixing browser speaker and injected
microphone audio. Both audio inputs use half gain in the mix to avoid clipping
during overlap; independent original WAV tracks are retained for inspection.

The recorder also retains chunked WAV audio and WebM display segments for debugging
and recovery. The combined file is encoded/muxed inside Rust/GStreamer during the
session, and finalized at recording stop or session close. Its manifest entry has
`kind="video.recording"`, `stream="session"`, and the included `audio_streams`.
Clients can download that path through the normal artifact API. A text-only
recording contains video without an audio track.

Direct recorded sessions retain files and SQLite metadata in
`./artifacts/<session-id>/` by default. Supply `artifact_dir` to choose another
location. The remote worker retains recordings in its configured artifact root;
an external MCP server does not delete recorded sessions on exit.

Use `recording=False` to opt out. Unrecorded direct sessions without `artifact_dir`
use temporary files and a 4096-event in-memory journal, removed on context exit.
`session.recording.start()` / `.stop()` control recording within a media-enabled
session; restarting creates a new combined file for that recording interval.
Recording branches run independently of model subscriptions. Recording failure
fails the session. Metadata can be replayed; live media subscriptions cannot.
Slow audio subscribers receive an explicit overrun error; video subscribers can
skip stale frames and report discontinuities.

The context manager finalizes recordings and terminates its owned processes. Each
session uses separate sockets, process environments, audio server, and display. Camera
access is leased. Host-level isolation of untrusted sites is the responsibility of
the deployment; the optional container worker provides a stronger boundary.

## WebMCP page tools and site adapters

[WebMCP](https://webmachinelearning.github.io/webmcp/) lets a page register tools
(`document.modelContext.registerTool`) that an agent calls instead of driving the UI.
Chromium 153 ships it behind a feature flag, which `SessionConfig(webmcp=True)` turns on.
Sites in Chrome's origin trial (Target, WordPress Playground) expose tools without the flag;
A&A lists, calls and journals page tools either way. See the
[live-site findings](docs/webmcp-real-sites.md) for what WebMCP does and does not change.

```python
async with Adapter(SessionConfig(recording=False, webmcp=True, target_url=URL)) as session:
    listing = await session.webmcp.tools()        # every frame; blocked frames are reported
    result = await session.webmcp.call("search", {"query": "pricing"})
```

Tool names, descriptions and results come from the page: they are marked `untrusted`
and must be treated as data, never instructions. Calls time out (default 30 s), cap
output at 256 KiB, and report `navigated` if the page unloads. Each page has a DevTools
observer, so `webmcp.tools_changed`, `webmcp.invoked` and `webmcp.responded` events
record every invocation — including ones made by the page's own code — with an
output preview and SHA-256. Cross-origin iframes expose tools only with `allow="tools"`. Survey a site with
`uv run python tools/webmcp_survey.py <url>`.

Sites without WebMCP have no page tools. A **site adapter** gives the harness the same
named-tool interface anyway: each tool is a list of ordinary browser operations with
`{{argument}}` placeholders, run with real Playwright input and journaled with
`tool_source="tester"`. Adapters need no flag and inject nothing into the page.

```python
from mba import AdapterStep, AdapterTool, SiteAdapter

await session.webmcp.add_adapter(SiteAdapter(
    name="chat", url_pattern="https://your-target.example/*",
    tools=[AdapterTool(
        name="ask", input_schema={"type": "object", "required": ["text"],
                                  "properties": {"text": {"type": "string"}}},
        steps=[AdapterStep(operation="fill", selector="textarea", value="{{text}}"),
               AdapterStep(operation="click", selector="button.send")],
    )],
))
await session.webmcp.call("ask", {"text": "Hello"})
```

The MCP server exposes `aa_webmcp_tools`, `aa_webmcp_call` and `aa_webmcp_adapter`;
native Python/TypeScript helpers are `webmcp_tools`/`webmcp_call` and
`webmcpTools`/`webmcpCall`. A tool named by both an adapter and the page resolves to
the adapter unless `source="site"` is passed.

## Remote CDP browsers (text only)

A hosted browser such as Browserbase can host a text session. A&A attaches over CDP
and reuses the remote default context instead of launching Chromium:

```python
from mba import RemoteBrowser

config = SessionConfig(
    recording=False, target_url=URL,
    remote_browser=RemoteBrowser(cdp_url=session_from_provider.connect_url),
)
```

Set `MBA_REMOTE_CDP_HOSTS` to the allowed host names (for example
`connect.browserbase.com`; `*.example.com` wildcards are accepted). The remote machine's
microphone, speaker, camera and display are not A&A's, so only tools-mode text sessions
without recording are accepted; WebMCP works there only for origin-trial sites because
launch flags cannot be set. The CDP URL and headers are never written to the manifest or
journal, and the HTTP worker, controller, remote SDK and MCP server reject the option.
For voice or video agents, run your own worker instead.

## Optional remote compatibility

Install the `remote` extra to use the existing HTTP SDK/controller:

```sh
uv sync --locked --extra remote
uv run python deploy/build.py
# Set MBA_TOKEN to a random value of at least 24 characters first.
uv run mba serve --root artifacts
```

Existing `mba.sdk.Client`, `/v1` action endpoints, and input WebSocket framing remain.
The workers now execute the same TypeScript/Rust runtime. `mba.protocol.SessionConfig`
retains legacy multimodal/calibration defaults; top-level `mba.SessionConfig` uses
manual text interaction by default. Both now enable full display recording. Choose
modalities explicitly when mixing APIs.

New remote methods: `session.browser_command(...)`, `session.capture(kind, ...)`, and
`session.recording(enabled)`. New output WebSockets use binary Protobuf packets.
Remote callers do not need Node, GStreamer, or local devices. Full-rate raw frames
need sufficient bandwidth; compressed remote realtime transport is not included.

## Build and validate

```sh
uv run python tools/generate.py --typescript --rust
uv run python tools/browser_build.py
uv run ruff check src tests tools deploy examples
uv run pytest -m 'not linux and not soak and not browser' -q
MBA_BROWSER_TESTS=1 uv run pytest tests/test_browser.py tests/test_library.py \
  tests/test_harness_interfaces.py tests/test_webmcp.py tests/test_remote_browser.py -q
npm run test:types --prefix harness-client
cargo test --locked --manifest-path media-core/Cargo.toml
```

Rust default features execute real GStreamer tests; `--no-default-features` runs only
portable protocol tests and produces no usable media backend. `tools/package_runtime.py`
builds a Linux release archive and prints its checksum. Install that explicitly with:

```sh
mba setup --media-archive dist/mba-media-0.3.0-linux-x86_64.tar.gz --sha256 CHECKSUM
```

On a prepared Linux host, run `tests/test_linux.py` for full virtual-device integration,
isolation, cancellation, recordings, and measured timing. The opt-in soak remains a
release gate. No real-site conversation or latency claim is implied by portable tests.

### Docker Desktop on Apple Silicon

Build the native ARM64 image and run real Linux audio/display checks:

```sh
python deploy/build.py --platform linux/arm64 --tag mba-worker:harness-arm64
python deploy/test-container.py --image mba-worker:harness-arm64
python deploy/test-http-container.py --image mba-worker:harness-arm64
python deploy/test-harness-container.py
```

The runner uses an unprivileged, disposable container with the deployment seccomp
profile. Evidence is retained under `.cache/container-artifacts`. It checks text,
browser microphone/speaker routing, streamed PCM resampling, interruption, display
capture, recording finalization, and concurrent-session audio isolation.
The external harness runner additionally verifies native Playwright control and a
separate MCP client sharing audio access. It confirms that MCP detachment preserves
the native controller and checks child-process cleanup before removing the worker.

Docker Desktop's tested LinuxKit kernel has `CONFIG_MEDIA_SUPPORT` disabled, so it
cannot run the v4l2loopback camera path. Camera acceptance requires a Linux host or
VM with V4L2 and the loopback module configured by `deploy/provision-host.sh`.
# A-A
