# Subscription-backed browser voice test

**Stopped experiment, not the intended A&A architecture.** The production A&A
worker owns only the target browser and its devices/capture. A realtime model,
ChatGPT integration, function, or audio-file producer is a downstream client
outside that worker. Signing in to ChatGPT inside A&A is not a requirement.
The two-browser setup below was a temporary wiring diagnostic and is not part
of the adapter library or its normal worker image.

`tools/browser_voice_pair.py` runs two library sessions inside a Linux worker.
One browser hosts ChatGPT Voice; the other hosts Docket. This is an interactive
adapter test, not a scenario engine or a vendor API connector.

The optional `deploy/Dockerfile.viewer` adds Ubuntu's noVNC, websockify and x11vnc
packages to `mba-worker:0.3.0-arm64`. Each browser has a password-protected viewer.
Publish container ports 6080 and 6081 **only on host 127.0.0.1**; never publish
the VNC ports. Keep the artifact mount private to the host user.

The runner expects `/artifacts` as its writable directory and a normal unprivileged
worker environment. It first tests two independent routes:

```
ChatGPT browser speaker → native GStreamer relay → Docket browser microphone
Docket browser speaker → native GStreamer relay → ChatGPT browser microphone
```

The Rust runtime owns each session's virtual devices and recording pipelines.
The test's two additional `gst-launch-1.0` processes carry the cross-session audio;
Python starts/stops them and does not forward individual PCM chunks. These extra
relays are test plumbing, not a new public Rust media API.

Before loading the websites, the runner uses browser oscillators and microphone
analysers to verify 440 Hz in one direction, 880 Hz in the other, and no self-echo.
Results are written to `preflight.json`. Both routes passed on Docker Desktop
Linux ARM64 on 2026-09-20. This verifies wiring, not latency or conversational quality.

Next, sign in to ChatGPT through its viewer. The temporary viewer password is in
`viewer-password.txt`. The ChatGPT account credentials remain in the ephemeral
browser; do not export cookies or copy authentication from the user's Mac browser.
Audio relays and recordings are off during sign-in. Session event diagnostics remain
enabled. The browser profiles disappear when the disposable container is removed.

Once ChatGPT is signed in, prepare its conversation instructions and start Voice.
Then atomically write `command.json` in the artifact directory containing:

```json
{"operation": "connect"}
```

This starts both recordings, connects the native audio routes, and clicks Docket's
**Talk to Aura** control. The conversation runs for at most 180 seconds, then the
runner closes both browsers and finalizes recordings. To stop early, write:

```json
{"operation": "stop"}
```

Before connection, the login viewer expires after 30 minutes. `status.json` reports
startup, preflight, login-wait, connected, closed, or failed state. A successful
preflight is **not** evidence that a GPT-Live ↔ Docket conversation occurred.
Verify both sites' visible conversation state and both recordings after connection.

The interactive experiment was stopped after correcting this architectural
boundary. No subscription-backed two-agent conversation was verified.
