# A&A protocols — library 0.3.0

## Internal process contract

`proto/adapter.proto` defines the versioned Browser and Media services. Python,
TypeScript, and Rust bindings are generated from it. Commands, lifecycle observations,
and binary media use gRPC over private Unix sockets. The startup protocol version is 1;
incompatible components fail before executing interactions. Extensible observation
payloads and site bindings use JSON within the typed envelope.

The Browser service owns Playwright. The Media service owns GStreamer and Linux devices.
Rust timestamps are mapped to the Python session clock using a short health RPC. Media
packets have a stream ID, sequence, presentation time, format, discontinuity flag, and
binary bytes. Input EOS is explicit; disconnecting before EOS cancels the track.

A native endpoint is a trusted session-internal interface. Use the Python coordinator
to reserve/prepare input actions before writing media directly to its socket.

## Optional HTTP compatibility contract


The API uses bearer authentication. Worker credentials are HMAC-derived per session.
All controller routes are under `/v1`. The controller provides OpenAPI documentation
for its own routes; the worker's OpenAPI schema contains the action/stream payloads.
The Python types in `mba.protocol` remain the HTTP request models; the internal
process contract is generated from Protobuf.

| Method | Route | Behavior |
|---|---|---|
| POST | `/sessions` | Create a container session; response after readiness |
| GET | `/sessions` | List controller-owned sessions |
| GET | `/sessions/{id}` | Manifest, lifecycle state and artifacts |
| POST | `/sessions/{id}/stop` | Drain and finalize; idempotent |
| POST | `/sessions/{id}/assets` | Raw asset upload, maximum 512 MiB |
| POST | `/sessions/{id}/actions` | Accept an action bundle |
| GET | `/sessions/{id}/actions/{action}` | Current/final status |
| POST | `/sessions/{id}/actions/{action}/cancel` | Cancel local injection |
| POST | `/sessions/{id}/streams` | Declare a live input stream |
| WS | `/sessions/{id}/streams/{stream}/ws` | One binary producer per stream |
| GET | `/sessions/{id}/events?after=N` | Up to 256 events after sequence N |
| WS | `/sessions/{id}/events/ws?after=N` | Replay, then live events |
| GET | `/sessions/{id}/artifacts/{path}` | Completed evidence files and logs |
| POST | `/sessions/{id}/checkpoint` | Full-viewport screenshot with time bounds |
| GET/POST | `/sessions/{id}/calibration` | Harness-only measurements / tone or visual command |

Action example:

```json
{
  "action_id": "utterance-1",
  "tracks": [
    {"kind": "audio", "source": {"kind": "asset", "asset_id": "0123456789abcdef0123456789abcdef"}, "offset_us": 0}
  ],
  "replace": false
}
```

`start_at_us` is optional and uses the session clock. Without it, playback starts after
preparation. A missed explicit start fails rather than silently shifting the schedule.
Track kinds are unique within a bundle. Text tracks have zero offset.

## Binary media

After connection the server returns `{"ready": true, "config": ...}`. Each binary
message consists of two unsigned 64-bit big-endian integers followed by media bytes:

```text
sequence (8 bytes) | relative PTS in microseconds (8 bytes) | media payload
```

- Sequence starts at zero and advances by one.
- Audio: `(rate / 50) × channels × 2` bytes of S16LE; PTS = sequence ×20,000.
  Supported rates are 16/24/48 kHz and channels 1/2. Existing default: 1920 bytes.
- Video: `width × height × bytes_per_pixel` bytes (RGB24=3, YUY2=2);
  PTS = floor(sequence ×1,000,000/fps). Bounds match the direct library.
  Existing default: 1280×720 YUY2 at 30 FPS.
- The server acknowledges accepted sequence and dropped-frame count.
- Send `{"end": true}` as a text message for EOS; wait for `{"ended": true}`.
- A disconnect before EOS aborts the stream. Streams are single-use.
- Audio backpressures at ten queued chunks; video keeps the three newest frames.
- The SDK paces producers to their declared rate. Audio underruns >100 ms fail the action.

Raw video requires substantial local/LAN bandwidth. Encoded remote realtime transport
is outside v1; use uploaded video assets when the link cannot sustain raw frames.

## Events

The common envelope is typed by `Event`; payload fields depend on `type` and remain
extensible for extractors. Built-in types include:

| Type | Payload |
|---|---|
| `session.state` | State and failure reason |
| `session.phase` | Startup phase |
| `action.status` | Action ID, state, accepted/started/ended times, error |
| `action.cancel_requested` | Action ID; event time marks request handling |
| `injection.started` | Action, stream, requested/actual time, boundary, uncertainty |
| `injection.completed` / `injection.cancelled` | Local drain estimate, uncertainty |
| `text.revision` | Document-scoped message ID, revision, content, DOM provenance |
| `text.removed` | Message ID removed from the observed DOM |
| `audio.segment.available` | Artifact path, stream, start/end, completeness |
| `video.segment.available` | Display recording path and interval |
| `frame.available` | Frame path, surface/reason, interval |
| `capture.gap` | Stream and gap/drop reason; interval/count when known |
| `capture.metrics` | Recording and sampler counters |
| `extraction.gap` / `extraction.failed` | Processor and affected evidence |

Observation timestamps are mapped from their producer clock. DOM events include
clock-mapping uncertainty. Events may arrive out of observation order; publication
sequence is strictly increasing. Reconnecting clients resume after their last sequence.

Camera/display/audio settings are observable configuration, not claims of physical
human equivalence. DOM text is evidence of the UI, STT is derived evidence, and neither
implies answer correctness or a completed conversation turn.


## New optional HTTP endpoints

| Method | Route | Behavior |
|---|---|---|
| POST | `/sessions/{id}/browser` | Explicit open/click/fill/press/wait/text/screenshot operation |
| POST | `/sessions/{id}/recording` | Start or stop recording with `{"enabled": true/false}` |
| WS | `/sessions/{id}/capture/{audio,video}/ws` | Live binary `MediaPacket` Protobuf messages |

All routes retain the `/v1` prefix and existing bearer authentication. Capture parameters
are `channels=1/2` and `fps=1..30`. Capture does not replay after reconnect. Errors are
sent as JSON followed by connection closure. Existing input WebSocket framing is unchanged.

Direct `mba.SessionConfig` defaults to manual text interaction and full display
recording. The direct adapter persists recorded sessions at `./artifacts/<session-id>`
unless an artifact directory is supplied. `recording=False` restores the lightweight
unrecorded path. Legacy `mba.protocol.SessionConfig` retains its calibration/multimodal
defaults. Omitted `visual` enables display capture whenever recording is enabled;
explicit `visual=False` remains an audio-only recording choice.

The final combined WebM is published as `video.recording.available` and included in
the manifest with `kind="video.recording"`, `stream="session"`, and `audio_streams`.
VP8 screen video and mixed Opus speaker/microphone audio share session-relative
timestamps. The file becomes a complete downloadable artifact only after EOS
finalization. Chunked `video.segment` and `audio.segment` evidence remains available.
Capture subscriptions do not define agent turns.


## Harness integration additions

Control mode is immutable: `browser_control=tools|native` (default `tools`).
Native sessions wait `native_attach_timeout_s` (default 120) for client setup.

| Method | Route | Behavior |
|---|---|---|
| GET | `/sessions/{id}/capabilities` | Actual session control/media capabilities |
| GET | `/sessions/{id}/native` | Setup/version/clock metadata; private endpoints and credentials omitted |
| WS | `/sessions/{id}/native/ws` | Authenticated opaque Playwright protocol, one controller |
| POST | `/sessions/{id}/native/observations` | Registered helper observation batches, max 256 rows/1 MB |
| POST | `/sessions/{id}/media/stop` | Cancel the current audio/video producer |

These routes retain the `/v1` prefix and bearer authentication. The native connection
is a separate channel, not a BrowserCommand serialization of Playwright operations.
The controller forwards it to the session worker without opening another public port.
The local library's descriptor contains its loopback endpoint and session credentials;
these must not be logged or placed in evidence manifests.

Browser commands additionally carry `page_id`, `frame_id`, `expected_document_id`,
and operation options. See `BrowserOperation` and the harness guide for supported
operations. Browser mutations are rejected in native mode at both the API and worker
boundaries. Native DOM events carry `provenance=native_helper` and timing uncertainty.
Downloads use the `downloads/` artifact namespace. Capabilities distinguish session
speaker/display capture from selected-page DOM observations.
