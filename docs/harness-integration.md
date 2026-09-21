# External harness integration

A&A runs the target browser and its devices. The calling harness runs outside it.
Provider authentication, realtime model connections, response generation, TTS, STT,
avatars, scenarios, and evaluation belong to that caller.

## Choose one controller at session creation

`SessionConfig(browser_control="tools")` is the default. The TypeScript worker owns
its Playwright context and executes the library's documented browser commands.

`SessionConfig(browser_control="native")` starts a native Playwright server. An
external Python or TypeScript client owns the contexts and pages it creates. The
A&A helper returns ordinary Playwright objects; locator chains, routing, evaluation,
uploads, downloads, dialogs, and popups remain normal Playwright operations.

The mode cannot change during a session. Native mode rejects worker browser commands
and text submissions with `browser_control_external`. Use the native `Page` for
those operations. Audio, camera, display capture, recording, and events remain on
the A&A session. One native controlling connection is admitted; additional clients
are rejected without disturbing that connection. Native disconnection ends the
session. A lost page/conversation is never silently recreated.

Full native compatibility requires the installed Playwright client and server to
share a major/minor version. This release is tested with **1.63.0** (Chromium 153). A harness that
hardcodes launching its own browser must be configured or adapted to connect instead.
There is no universal redirect of another harness's Playwright calls.
In native mode, use the raw `Page.screenshot()` for a browser screenshot; the worker's
browser screenshot tool is available in tools mode. Native media sessions can still
receive desktop pixels through `session.visual.frames()` independently of Playwright.

## Installation

Python 3.12–3.13 is supported by the pinned dependency set:

```sh
uv sync --locked --extra dev --extra remote --extra native --extra mcp
npm ci --prefix browser-worker
uv run python tools/browser_build.py
uv run mba setup --browser
npm ci --prefix harness-client
```

The `native`, `remote`, and `mcp` extras are optional. Base library import and tools
mode do not import Python Playwright or MCP. The TypeScript helper package is in
`harness-client/`; install that local package in the harness project together with
`playwright@1.63.0`. Native Linux workers do not need the Python Playwright client or
an MCP server installed inside their image.

Full display recording is enabled by default, so normal sessions run on the Linux
worker. Set `recording=False` (Python) or `recording: false` (TypeScript) explicitly
for unrecorded local Mac text sessions. Direct recorded sessions persist under
`./artifacts/<session-id>/`; remote recordings use the worker artifact root.

The completed recording is one WebM with the rendered display and, in audio-enabled
sessions, a mix of speaker and injected microphone audio. Original audio tracks and
display segments remain separate diagnostic artifacts. The final file becomes
available when recording stops or the session closes:

```python
manifest = await session.stop()
video = next(a for a in manifest["artifacts"] if a["kind"] == "video.recording")
# For a remote session:
await session.download(video["path"], Path("session.webm"))
```

Import `Path` from `pathlib` in the calling script. Starting recording again creates
a new combined file for the new recording interval.

## Direct Python native session

```python
from mba import Adapter, SessionConfig

async with Adapter(SessionConfig(browser_control="native")) as session:
    async with session.connect_native() as native:
        # native.browser, native.context, and native.page are real Playwright objects.
        await native.page.goto("https://your-target.example")
        await native.page.get_by_role("button", name="Start").click()
        await native.bind_text(native.page, message_selector=".assistant-message")
        await native.page.get_by_role("textbox").fill("Hello")
        await native.page.get_by_role("textbox").press("Enter")
```

Selectors are supplied by the caller. `register_context(context)` applies session
permissions and tracks its pages. `select_page(page)` selects the conversation page.
`bind_text(page, ...)` enables observations for that page's configured surface.
Additional pages remain fully controllable without registration, but they do not
implicitly replace the selected conversation page.

Native setup has a configurable 120-second initial attachment/setup deadline. The
session may be infrastructure-ready before it is target-ready. Media injection
requires the helper to finish registering its target. The helper uses the configured
URL, permissions, storage state, and optional declarative join/ready selectors.

Explicit helper exit closes client contexts and ends the A&A session. A crashed or
disconnected native controller produces a terminal session with its failure reason.

## Remote Python: external harness, Linux worker

```python
import asyncio
import os
from mba import AudioFormat, SessionConfig
from mba.sdk import Client

async with Client(os.environ["MBA_URL"], os.environ["MBA_TOKEN"]) as client:
    session = await client.start_session(SessionConfig(
        browser_control="native", mode="audio", camera=False,
        target_url="https://your-target.example",
    ))
    async with session.connect_native() as native:
        await native.page.get_by_role("button", name="Start call").click()

        async def receive():
            source = session.audio.capture(channels=1)
            try:
                async for chunk in source:
                    await your_external_consumer(chunk)
            finally:
                await source.aclose()

        async def send():
            async with session.audio.open_input(AudioFormat(rate=24000)) as mic:
                async for pcm in your_external_source():
                    await mic.write(pcm)  # exactly 20 ms: 960 bytes at 24 kHz mono

        async with asyncio.TaskGroup() as group:
            group.create_task(receive())
            group.create_task(send())
```

The two `your_external_*` functions are supplied by the harness. Termination and
turn-taking are its responsibility. A&A does not authenticate to or generate replies
with a model. `Bridge` also accepts remote sessions; native sessions cannot route
bridge text output through worker-owned UI actions.

Remote audio accepts S16LE 16/24/48 kHz mono/stereo. Camera inputs accept the same
RGB24/YUY2 formats and bounds as the direct library. Binary framing stays compatible:
sequence and relative PTS followed by bytes. Old 48 kHz mono/YUY2 defaults still work.

## TypeScript native client

```typescript
import {Client} from '@mba/harness-client';

const client = new Client(process.env.MBA_URL!, process.env.MBA_TOKEN!);
const session = await client.start({
  browser_control: 'native', mode: 'text',
  binding: {kind: 'manual'},
});
await using native = await session.connectNative();
await native.page.goto('https://your-target.example');
await native.page.getByRole('button', {name: 'Start'}).dblclick();
await native.bindText(native.page, {message_selector: '.assistant-message'});
```

The small TypeScript package supplies native setup, observation forwarding, session
control, and typed raw Playwright objects. Continuous media can be connected through
the binary API from any external connector; this package does not wrap provider SDKs.

## Browser tools

Both Python session implementations expose `browser.command(operation, ...)` and
convenience methods for opening, clicking, filling, keypresses, waiting, screenshots,
snapshots, page selection, and bindings. Expanded commands include double-click,
hover, drag, scroll, form selection/checking, uploads, downloads, dialogs, and history.

```python
snapshot = await session.browser.snapshot()
await session.browser.command(
    "click", expected_document_id=snapshot["document_id"],
    options={"target": {"kind": "role", "role": "button", "name": "Start"}},
)
await session.text.bind({
    "kind": "manual", "input_selector": "textarea",
    "submit_selector": "button.send", "message_selector": ".assistant",
})
```

Targets support CSS, role/name, text, and test ID. A snapshot includes accessibility
text, page and document IDs, viewport and frame inventory. Use top-level `page_id` and
`frame_id` to address a specific surface. A stale expected document fails explicitly.
Browser mutations are ordered; reads and media work independently. The tools API is
finite and does not provide arbitrary evaluation; native mode provides that capability.

`upload` takes a registered asset ID as its value. `downloads` lists completed download
paths retrievable through the artifact API. Configure `dialog` handling before the
operation that triggers a dialog; the default dismisses it. Selecting another page
clears the conversation binding, so bind it explicitly for the new page.

## MCP stdio

```sh
# Direct local library (text works on Mac; full media requires Linux).
mba mcp --transport stdio

# MCP stays next to the harness; target/browser/devices run in the remote worker.
# MBA_TOKEN is supplied through the harness's environment configuration.
mba mcp --transport stdio --worker-url http://127.0.0.1:8090
```

Discover the `aa_*` tools through MCP. They cover session create/attach/inspect/close,
browser commands, text bindings, screenshots, asset playback, stream creation,
recording, bounded audio capture, action status/cancel, events, and artifact reads.
Screenshots return image content plus a structured artifact handle. Bounded audio
capture (up to 60 seconds) returns an action ID; poll its status for the artifact,
then call `aa_artifact_read` to receive MCP audio content. Artifact reads are capped
at 8 MiB; use the worker's artifact download interface for larger recordings.

Long-running media is never transported through a sequence of LLM tool calls.
`aa_media_open` prepares an action and returns a stream descriptor. Remote descriptors
refer to the configured worker and its authenticated binary WebSocket path. Local
Linux descriptors use the session's private gRPC socket and prepared stream ID.
The external connector sends media and EOS; it must preserve the documented framing.
No model credentials are required. Cancel prepared actions when abandoning a stream.

MCP shutdown cancels its own capture jobs and closes sessions it created. It does not
close sessions merely attached by ID. Explicit `aa_session_close` always closes the
requested session. Logs use stderr; stdout is exclusively MCP protocol traffic.

## Observation and capture boundaries

- DOM events from native helpers identify their provenance and timing uncertainty.
  They are client observations, not proof of a server-side action or answer quality.
- Native Playwright calls are not individually rewritten into A&A action receipts.
  The harness can collect its own Playwright trace.
- Speaker capture includes all audible pages in the session. Display capture shows
  the rendered desktop. Neither is automatically a per-tab media stream.
- Camera injection and browser screen sharing are distinct; screen sharing is not
  implemented by these interfaces.
- Client-local files and worker files are distinct. Use Playwright's supported remote
  file APIs or A&A assets/artifact downloads instead of assuming shared paths.
- Camera device tests require a V4L2-enabled Linux host. Docker Desktop on this Mac
  supports the audio/display paths but does not supply the camera subsystem.

## Validation commands

```sh
MBA_BROWSER_TESTS=1 uv run pytest tests/test_browser.py tests/test_library.py tests/test_harness_interfaces.py -q
uv run pytest -m 'not browser and not linux and not soak' -q
npm run check --prefix browser-worker
npm run test:types --prefix harness-client
uv run python deploy/build.py --platform linux/arm64 --tag mba-worker:harness-arm64
uv run python deploy/test-harness-container.py
uv run python deploy/test-container.py --image mba-worker:harness-arm64
```

`deploy/test-harness-container.py` is an external caller that measures live browser
microphone/speaker routing and rendered capture. Its target page is a measurement
fixture; there is no model simulator or scenario system inside the worker.
It also attaches a separate MCP stdio client for bounded audio capture, verifies
that detaching it preserves the native controller, and checks process cleanup.
See [validation](validation.md) for measured results and remaining camera/soak gates.
