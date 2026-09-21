# Verification boundary — 0.2.0 external harness integration

Last verification: 2026-09-20. All executed checks below passed. This is not yet a
full Linux media acceptance sign-off.

Development environment: macOS arm64. Python 3.12, Node.js 26, a project-local Rust
1.98.1 toolchain, and Homebrew GStreamer 1.28.7 were used. The reference deployment
remains Ubuntu 24.04 x86-64. Docker Desktop 4.91.0, Engine 29.8.0, and Compose 5.5.1
are now installed. The worker was built and tested as Ubuntu 24.04 Linux ARM64 inside
Docker Desktop on this Mac. The VM kernel is `7.0.12-linuxkit`.

## Executed locally

| Check | Result |
|---|---|
| Portable Python suite | 59 passed, 1 native-service test skipped |
| Actual Chromium integrations | 10 passed (18 in the combined browser/library/harness run, including 8 portable checks) |
| Rust native-feature suite | 6 passed |
| Python-to-Rust Unix-socket integration | 1 passed against the real native service |
| TypeScript strict type checking and bundled worker build | Passed |
| TypeScript native client type checks and distributable package | Passed |
| Python lint and compilation | Passed |
| Generated Python/TypeScript bindings, browser bundle and runtime manifests | Regeneration produced identical files |
| Generated Rust binding | Build checks checked-in source against generated output |
| Wheel build | Passed; generated protocol and bundled worker/manifests included |
| Clean wheel installation | Dependency check, base imports, real Chromium session and shutdown passed |
| Deployment shell scripts | Syntax checks and arbitrary non-root UID entrypoint check passed |
| Linux audio/display container acceptance | Passed; real devices and Chromium, no fake media |
| Production HTTP worker container | Passed; remote SDK, artifacts, events, and cleanup |
| External Python native controller + remote MCP stdio client | Passed against real Linux speaker/microphone/display devices |
| Camera-dependent Linux acceptance suite | 5 remain gated on a V4L2-enabled host |

Browser tests use the public Python API to start the actual TypeScript worker and
Chromium. They cover iframe DOM revisions, navigation, direct controls, screenshots,
two concurrent sessions, protocol mismatch, browser death, and startup cleanup.
The harness tests also cover native Python and TypeScript objects, interception,
uploads, pages, frame controls, text rebinding across navigation, document guards,
single-controller authentication, native disconnect cleanup, real MCP discovery,
schema validation, and MCP image responses.

Rust tests use real GStreamer pipelines for WAV decoding and 24→48 kHz resampling,
and live RGB→YUY2 camera-frame conversion. They also check packet ordering/format
validation and actual 10-second WAV chunk boundaries with a final tail.
The video recording test finalizes a real WebM file and decodes all 12 submitted
frames. It does not require or simulate a virtual display or camera.
The combined-recording test decodes both VP8 video and Opus audio, and verifies
distinct speaker and injected tones at their expected timestamps, including overlap.
Homebrew's plugin scanner emitted optional Python/GTK-plugin warnings; the required
conversion pipelines and tests passed. Those tests do not emulate Linux devices.

Portable tests cover action lifecycle, persistence/replay, bounded event history,
legacy input framing, authentication, controller leases, optional camera allocation,
connector routes/cancellation, callback failure, new remote output packets, and
checksum/version/path validation of runtime archives.

The clean wheel test used its installed Python package and bundled TypeScript
worker with the existing locked Node dependencies and downloaded Chromium. It
verified navigation, field input, PNG capture, clean worker exit and socket removal.
FastAPI, httpx, Python Playwright and GI were absent from that environment. Fresh
browser downloads and Linux runtime installation were not exercised by that test.
The final wheel is `dist/multimodal_browser_adapter-0.2.0-py3-none-any.whl`; the native
TypeScript helper is `dist/mba-harness-client-0.2.0.tgz`. Neither has been published.

## Full recording defaults and cache cleanup

Full display recording is now the default for all interaction modes. Direct
recorded sessions persist under `artifacts/<session-id>` unless another root is
supplied. Tests of the lightweight Mac browser path explicitly opt out with
`recording=False`; the Linux tests omit recording and visual flags to exercise the
new defaults. The standard HTTP test verifies that a default text session produces
a downloadable video-only full WebM. The external audio harness test verifies a
single full WebM containing VP8 video and mixed Opus audio, and decodes it with FFmpeg.
Independent WAV tracks and display segments remain available alongside the full file.

The new default also exposed a headed text-only Chromium shutdown stall. Both
control modes now use the same owned browser-server process and its confirmed-exit
stdio cleanup. Tools mode still has one internal controller; native mode still has
one external controller. Browser regression and Linux HTTP shutdown checks pass.

At the user's request, 111 old cached audio/video recordings (84.8 MiB) were deleted.
The historical checks below retain their reports and screenshots, but their cached
recording files have been removed. Input fixtures, dependency caches, source files,
and build artifacts were retained. Verification recordings generated during this
change are also cleared from the cache after the final sample is retained under
`artifacts/`. The cleanup inventory is `.cache/recording-cleanup-report.json`.
The completed cleanup removed another 20 verification files (6.7 MiB), for 131
cached recordings total. The retained sample is
`artifacts/6597ee47f8074f7398f363f9a64d0a89/recordings/session-1298619.webm`.
It contains 7.66 seconds of VP8 screen video and mixed Opus audio; its sibling
`verification.json` records the decoding, remote download, and process cleanup
checks. The final image ID is recorded in `deploy/build-manifest.json`.

## Docker Desktop execution

`mba-worker:harness-arm64` is built locally. `deploy/build-manifest.json` records its
image ID and immutable Ubuntu/Node base references. Commands to reproduce:

```sh
python deploy/build.py --platform linux/arm64 --tag mba-worker:harness-arm64
python deploy/test-container.py --image mba-worker:harness-arm64
python deploy/test-http-container.py --image mba-worker:harness-arm64
python deploy/test-harness-container.py
```

The audio/display test runs an unprivileged container with all capabilities dropped,
the deployment seccomp profile, and Chromium's sandbox enabled. It verified:

- Text input and DOM response observation against a local measurement page.
- Browser speaker audio captured as native PCM.
- WAV injection and live 24 kHz PCM resampling reaching the browser microphone.
- Microphone silence following interruption.
- Actual rendered red pixels in display frames and PNG screenshots.
- Final WAV/WebM recordings decoded successfully by FFmpeg.
- Two concurrent sessions: an injected tone reaches one microphone while the other
  remains silent.
- No remaining Node, Chromium, Rust media, Xvfb, Openbox, or PulseAudio processes
  after both sessions close.

The HTTP test uses the production entrypoint, the host user's numeric UID, an
authenticated localhost-only endpoint, and the optional remote SDK. It verified
browser controls, PNG artifact download, persisted event replay, session shutdown,
and removal of its disposable worker container. Evidence and logs are retained in
`.cache/container-artifacts`, `.cache/http-container-artifacts`, and
`.cache/container-test.log` / `.cache/http-container-test.log`.

The final harness-image reruns are recorded in `.cache/harness-regression.log` and
`.cache/harness-http-test.log`. The external harness run's full report is
`.cache/harness-container-artifacts/report.json`, session
`64be2d93e5ac4efc8168136c41424128`. It verified:

- Python outside Docker controls the worker's browser using native Playwright.
- Browser speaker audio is received through the external binary SDK.
- A separate, real MCP stdio client attaches to that session, captures exactly two
  seconds of 48 kHz mono audio, and returns a valid WAV in MCP `AudioContent`.
- Closing the attached MCP client leaves native Playwright connected and usable.
- Stereo 24 kHz input is resampled and reaches the browser's microphone at the
  expected frequency; cancellation restores measured silence.
- Rendered RGB frames reach the external SDK, recordings finalize, and all owned
  browser/media/display/audio child processes exit before the container is removed.

This run reported no capture gaps or forced browser shutdown. For 20 trivial native
`page.evaluate` round trips, median latency was 2.42 ms and p95 was 4.58 ms. The audio
cancel acknowledgement took 7.18 ms. These are local Mac-to-Docker control timings,
not acoustic latency or a residual-audio guarantee. The release timing gates below
still require the stated end-to-end measurements.

Docker Desktop's kernel reports `# CONFIG_MEDIA_SUPPORT is not set`. Thus this
environment does not provide the V4L2 subsystem needed by v4l2loopback. The camera
tests have not been replaced with browser fake-device flags.

## Defects fixed during this audit

- Interrupting an in-flight media write could terminate the connector output loop.
  Each delivery now has a cancelable task, allowing the next response to proceed.
- Streams were removed before EOS draining finished, making them unreachable by
  interruption. They now remain registered until draining completes.
- Cancellation during a shutdown tail or browser stop could bypass native cleanup.
  Cleanup now always cancels observers/feeds, reaps both child process groups, and
  releases camera leases and socket directories.
- Passing an audio format to the camera (or the reverse) could create the wrong
  input stream. Crossed formats now fail immediately; camera capture explicitly
  directs callers to `visual.frames()` instead of silently returning speaker audio.
- Python gRPC's default Unix-socket authority was rejected by Rust's HTTP/2 stack.
  Private channels now explicitly use `localhost`; a real cross-language test
  reproduces the prior failure and verifies the fix.
- GStreamer's X11 source probes the display during pipeline linking. Its display
  name is now set before linking, preventing stale startup errors.
- The microphone feeder now waits for bounded queue capacity during PulseAudio
  startup instead of terminating on an idle-data overflow. Prolonged stalls still fail.
- Each PulseAudio process now has its own runtime directory, avoiding shared PID-file
  conflicts between concurrent sessions in the same worker.
- The image now supports ARM64, creates the X11 socket directory for non-root use,
  and resolves the NSS wrapper library without an Intel-specific path.
- Seccomp now permits Chromium's namespace-local `chroot` call while preserving
  dropped container capabilities and the Chromium sandbox. See `deploy/NOTICE.md`.
- Docker 29 emits lowercase missing-container errors. Cleanup/reconciliation now
  accepts both capitalization variants and still propagates daemon failures.
- Native headed Chromium could exit while descendant-held stdio pipes kept
  Playwright's close promise pending. The worker now closes those streams after the
  owned browser's confirmed exit, and keeps shutdown observation active through Stop.
- Observer configuration is inserted through a template placeholder, so selectors
  containing semicolons or replacement-string tokens cannot corrupt the script.
  Native rebindings carry a generation so old init scripts cannot supersede the
  newest binding after navigation.
- MCP artifact reads return typed audio/image/resource content instead of duplicating
  binary content inside JSON text; tool discovery retains nested schema definitions.

Regression tests reproduced the interruption and shutdown failures before the
fixes, and pass afterward. Connector teardown also attempts every stream cleanup
and provider close even when another cleanup fails.

The portable run reports a dependency deprecation warning from Starlette/AnyIO;
the native run reports the optional Homebrew scanner warnings described above.

## Public-site smoke checks

### Adaptive Docket voice conversation — 2026-09-20

Session `8b13a8e5b0684b3690b85754f3a5ab97` completed using the existing Linux image
and the external interactive runner `examples/docket_conversation.py`. An external
assistant selected follow-ups from Docket's rendered transcript and screenshots;
macOS speech synthesis supplied audio through the public A&A API. There was no
model provider, speech generator, or conversation policy inside the worker.

Eleven spoken utterances (ten conversational commands plus an interruption) all
appeared in Docket's displayed transcription. The target answered the interruption,
displayed its dashboard and visitor list, and acknowledged the final goodbye after
the caller declined an email follow-up. Opening a sample transcript did not finish:
Aura reported an obstructing overlay. A 55-second response wait elapsed during that
attempt; capture and subsequent conversation continued.

The combined WebM is 645.397 seconds long with VP8 video and mixed Opus audio from
speaker and injected microphone. FFmpeg decoded the entire recording, and exported
middle/end frames were visually inspected. Six display timestamp-gap events were
reported, the largest interval 689.837 ms; no audio capture gaps or forced browser
shutdown events were reported. The worker was removed. This does not establish
gapless capture, acoustic interruption latency, or native realtime-model streaming.

Persistent evidence is under `artifacts/8b13a8e5b0684b3690b85754f3a5ab97/`:
`conversation.webm`, `transcript.md`, `transcript.json`, `dom-transcript-final.json`,
`session-report.md`, `report.json`, `events.json`, screenshots, and original media.
Transcript timestamps distinguish microphone action start from DOM observation.

### Public Docket preflight — 2026-09-20

Opened `https://www.docket.io/` in the ARM64 Linux worker with the library's
TypeScript/Playwright browser and Rust/GStreamer audio/display runtime. Dismissed
the cookie banner and activated **Talk to Aura**. The rendered page showed Aura's
greeting and active call controls. Native speaker capture delivered 1,750 20-ms
chunks: 35 seconds, 48 kHz, mono PCM S16LE, peak absolute sample 15,021. The session
closed successfully and finalized 18 artifacts.

Local evidence: `.cache/docket-preflight.log`,
`.cache/container-artifacts/docket-preflight/docket-output.wav`, and session
`c72b1575aa8a4508844d07d698c1e78e` under that artifact directory.

That earlier run verified public-site startup and output capture only. Startup
feeder-gap events were observed; it did not establish latency guarantees or
microphone comprehension. The external fixture test below extends that coverage.

### External harness and audio fixture — 2026-09-20

An external Python harness controlled the Linux browser through native Playwright.
The short WAV fixture was generated on the Mac, then uploaded through the public
library and played into the worker's virtual microphone. No speech generator,
model provider, model login, or conversation policy was installed in the worker.

The question was: “Hello. In one sentence, what does Docket do?” The rendered target
transcript showed the question, followed by an answer describing Docket's handling
of website conversations, qualification, and sales handoff. The final screenshot
was visually inspected; the ordinary document body extraction did not include the
widget's transcript. This is evidence of one successful fixture-driven exchange,
not a GPT Live conversation, multi-turn reliability, or a normalized DOM transcript
test for this widget.

Session `2488d347a7814617acc7e79e1253a92d` received 2,190 speaker chunks (43.8 seconds
of 48 kHz mono PCM), with peak sample 20,102. There were 417 chunks above the simple
amplitude threshold after input completion. This threshold only measures audible
activity; it is not speech recognition. The session closed, finalized 15 recording
artifacts, and reported no capture gaps.

Evidence is in `.cache/docket-native-artifacts/`: `after.png`, `output.wav`,
`report.json`, and `events.json`. The external smoke script is
`.cache/docket-native.py`; the generated input is `.cache/docket-question.wav`.
The final run waited for DOM readiness and widget initialization. Earlier attempts
timed out on full-page loading or before the voice widget produced output, so this
smoke test does not establish a reliable generic join procedure for arbitrary sites.

## Pending Linux release gates

- Run camera input and streaming video injection through v4l2loopback with Chromium.
- Run the original combined full-duplex audio/camera/display acceptance suite and
  two-worker camera isolation tests on a V4L2-enabled Linux host.
- Measure p95 audio onset ≤150 ms, AV skew ≤100 ms, cancellation residual ≤200 ms.
- Run the 30-minute/two-worker soak and 100 repeated lifecycle checks.
- Build/install the Linux native runtime archive on a clean host.

Use `MBA_LINUX_TESTS=1 pytest tests/test_linux.py`; enable `MBA_SOAK_TESTS=1` separately.
Failures are implementation feedback, not permission to substitute browser fake media.
Native timing requirements are targets until measured on that host.

## Deliberate boundaries

- No scenarios, personas, evaluation, scheduling platform, or frontend.
- No vendor realtime connector; generic connector/callback contracts and a deterministic
  credential-free example are included.
- No automatic target UI discovery, generated avatar, or browser screen-sharing flow.
- Full media is Linux-only; macOS supports text/browser controls and screenshots.
- Remote full-rate video is raw binary and requires sufficient bandwidth.
- Camera/display/audio settings do not prove physical-human equivalence.
- Recording and injection timestamps identify local measurement boundaries. Injection
  completion is not proof that the remote target heard or understood the input.
- Asset paths and runtime sockets are trusted library interfaces, not a public multi-tenant
  security boundary. Deploy untrusted sites in suitably isolated workers.
