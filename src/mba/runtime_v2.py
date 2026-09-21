"""Library-managed TypeScript browser and Rust media subprocesses."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import platform
import tempfile
from pathlib import Path

from mba.browser_rpc import BrowserDriver
from mba.generated import adapter_pb2 as pb
from mba.generated import adapter_pb2_grpc as rpc
from mba.ipc import Child
from mba.protocol import AdapterError
from mba.setup import runtime_root


class NativeRuntime:
    def __init__(self, streams=None, camera=None, harness_url=None):
        self.streams, self.camera = streams, camera
        self.browser = self.media = self.media_stub = None
        self.tasks = []
        self.feeds = {}
        self.error = None
        self.lease = None
        self.socket_dir = None
        self.prepared = asyncio.Condition()
        self.native_streams = {}
        self.clock_ready = asyncio.Event()
        self.media_stopped = asyncio.Event()
        self.recording_changed = asyncio.Event()

    async def start(self, config, evidence):
        self.config, self.evidence = config, evidence
        (evidence.root / "logs").mkdir(exist_ok=True)
        # Short private paths also work under the macOS Unix socket path limit.
        self.socket_dir = tempfile.TemporaryDirectory(prefix="mba-", dir="/tmp")
        sockets = Path(self.socket_dir.name)
        media_needed = config.mode != "text" or config.camera_enabled or config.visual_enabled
        environment = {}
        if media_needed:
            if platform.system() != "Linux":
                raise AdapterError(
                    "linux_required", "Audio/video devices require a Linux execution host", 503
                )
            camera = self._lease_camera() if config.camera_enabled else ""
            installed = runtime_root() / "mba-media"
            executable = os.environ.get(
                "MBA_MEDIA_BINARY", str(installed) if installed.is_file() else "mba-media"
            )
            self.media = Child(sockets / "media.sock")
            await self.media.start([executable], {}, evidence.root / "logs/media.log")
            self.media_stub = rpc.MediaStub(self.media.channel)
            self.offset = 0
            # Subscribe before Start so startup/recording observations cannot be lost.
            self.media_events = self.media_stub.Observe(pb.Empty())
            await self.media_events.initial_metadata()
            self.tasks.append(asyncio.create_task(self._observe_media()))
            ready = await self.media_stub.Start(
                pb.MediaStart(
                    protocol_version=1,
                    audio=config.mode in {"audio", "multimodal"},
                    camera=config.camera_enabled,
                    visual=config.visual_enabled,
                    camera_device=camera,
                    root=str(evidence.root.resolve()),
                    recording=config.recording,
                ),
                timeout=config.startup_timeout_s,
            )
            before = evidence.clock.now_us()
            clock = await self.media_stub.Health(pb.Empty(), timeout=5)
            after = evidence.clock.now_us()
            self.offset = (before + after) // 2 - clock.now_us
            self.clock_ready.set()
            if ready.protocol_version != 1:
                raise AdapterError("protocol_mismatch", "Media protocol mismatch")
            environment = dict(ready.environment)
            self.tasks.append(asyncio.create_task(self._media_health()))
        self.browser = BrowserDriver(sockets / "browser.sock", environment)
        self.browser.on_failure = getattr(self, "on_failure", None)
        await self.browser.start(config, evidence)

    def _lease_camera(self):
        candidates = [self.camera] if self.camera else ["/dev/video10", "/dev/video11"]
        lease_dir = Path(f"/tmp/mba-cameras-{os.getuid()}")
        lease_dir.mkdir(mode=0o700, exist_ok=True)
        for camera in candidates:
            if not Path(camera).exists():
                continue
            f = (lease_dir / (Path(camera).name + ".lock")).open("a+")
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                f.close()
                continue
            self.lease = f
            return camera
        raise AdapterError("camera_unavailable", "No available configured v4l2loopback camera", 503)

    async def _observe_media(self):
        try:
            await self.clock_ready.wait()
            async for event in self.media_events:
                data = json.loads(event.json)
                if event.type == "media.stopped":
                    self.media_stopped.set()
                if event.type == "recording.state":
                    self.recording_changed.set()
                if event.type in {
                    "recording.segment.available",
                    "audio.segment.available",
                    "recording.available",
                }:
                    path = Path(data["path"]).resolve()
                    if not path.is_relative_to(self.evidence.root.resolve()):
                        raise ValueError("media artifact outside session")
                    kind = {
                        "audio.segment.available": "audio.segment",
                        "recording.segment.available": "video.segment",
                        "recording.available": "video.recording",
                    }[event.type]
                    await self.evidence.artifact(
                        path,
                        kind,
                        max(0, data.get("start_us", event.observed_at_us) + self.offset),
                        max(0, data.get("end_us", event.observed_at_us) + self.offset),
                        stream=data.get("stream", "display"),
                        **(
                            {"audio_streams": data.get("audio_streams", [])}
                            if event.type == "recording.available"
                            else {}
                        ),
                    )
                else:
                    if event.type == "capture.metrics":
                        self.evidence.update(metrics=data)
                    await self.evidence.emit(
                        event.type, data, max(0, event.observed_at_us + self.offset)
                    )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.error = str(exc)

    async def _media_health(self):
        try:
            while True:
                await asyncio.sleep(0.5)
                await self.media_stub.Health(pb.Empty(), timeout=3)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.error = str(exc)

    def check_health(self):
        if self.error or (self.browser and self.browser.error):
            raise RuntimeError(self.error or self.browser.error)
        if self.browser:
            self.browser.child.check()
        if self.media:
            self.media.check()

    async def prepare(self, action):
        for track in action.tracks:
            if track.kind == "text":
                continue
            if not self.media_stub:
                raise AdapterError("unsupported_modality", "This session has no media engine", 422)
            spec = pb.Track(action_id=action.action_id, kind=track.kind)
            if track.source.kind == "asset":
                spec.path = str(self.evidence.asset(track.source.asset_id).resolve())
            else:
                spec.stream_id = track.source.stream_id
                if spec.stream_id in self.native_streams:
                    native = self.native_streams[spec.stream_id]
                    spec.format.CopyFrom(native.format)
                else:
                    stream = self.streams.claim(spec.stream_id, track.kind)
                    spec.format.CopyFrom(
                        pb.MediaFormat(
                            kind=track.kind,
                            encoding=stream.config.format,
                            rate=stream.config.rate,
                            channels=stream.config.channels,
                            width=stream.config.width,
                            height=stream.config.height,
                            fps=stream.config.fps,
                        )
                    )
            await self.media_stub.Prepare(spec, timeout=20)
            if spec.stream_id:
                if spec.stream_id in self.native_streams:
                    self.native_streams[spec.stream_id].ready.set()
                else:
                    self.feeds[(action.action_id, track.kind)] = asyncio.create_task(
                        self._feed_legacy(stream)
                    )

    async def _feed_legacy(self, stream):
        async def packets():
            sequence = 0
            pts = 0
            last_source_sequence = -1
            async for packet in stream.packets():
                if packet.sequence != last_source_sequence + 1:
                    await self.evidence.emit(
                        "input.gap",
                        {
                            "stream_id": stream.id,
                            "dropped": packet.sequence - last_source_sequence - 1,
                        },
                    )
                last_source_sequence = packet.sequence
                yield pb.MediaPacket(
                    stream_id=stream.id,
                    sequence=sequence,
                    pts_us=packet.pts_us,
                    data=packet.data,
                )
                sequence += 1
                pts = stream.config.pts(packet.sequence + 1)
            yield pb.MediaPacket(stream_id=stream.id, sequence=sequence, pts_us=pts, end=True)

        await self.media_stub.Input(packets())

    async def play(self, action_id, track, start_us):
        if track.kind == "text":
            await self.browser.send_text(track.content)
            return
        play = self.media_stub.Play(
            pb.Track(action_id=action_id, kind=track.kind, start_us=max(0, start_us - self.offset))
        )
        feed = self.feeds.get((action_id, track.kind))
        if feed:
            await asyncio.gather(play, feed)
        else:
            await play

    async def cancel(self, action_id):
        if self.media_stub:
            await self.media_stub.Cancel(pb.ActionRef(action_id=action_id), timeout=2)
        for (aid, _), task in list(self.feeds.items()):
            if aid == action_id:
                task.cancel()

    async def release(self, action_id):
        if self.media_stub:
            await self.media_stub.Release(pb.ActionRef(action_id=action_id), timeout=2)
        for key in list(self.feeds):
            if key[0] == action_id:
                task = self.feeds.pop(key)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def capture(self, kind, channels=2, fps=0):
        if not self.media_stub:
            raise AdapterError("unsupported_modality", "Media capture is disabled", 422)
        call = self.media_stub.Capture(pb.CaptureRequest(kind=kind, channels=channels, fps=fps))
        try:
            async for packet in call:
                packet.pts_us = max(0, packet.pts_us + self.offset)
                yield packet
        finally:
            call.cancel()

    async def record(self, enabled):
        if not self.media_stub:
            raise AdapterError("unsupported_modality", "Media recording is disabled", 422)
        self.recording_changed.clear()
        await self.media_stub.Record(pb.RecordingRequest(enabled=enabled), timeout=12)
        await asyncio.wait_for(self.recording_changed.wait(), 3)

    async def stop(self, tail_s):
        failures = []
        try:
            if tail_s and self.media:
                await asyncio.sleep(tail_s)
            try:
                if self.browser:
                    await self.browser.stop()
            except Exception as exc:
                failures.append(str(exc))
            try:
                if self.media_stub and self.media.process.returncode is None:
                    await self.media_stub.Stop(pb.Empty(), timeout=10)
                    await asyncio.wait_for(self.media_stopped.wait(), 3)
            except Exception as exc:
                failures.append(str(exc))
        finally:
            # Tail waits and browser shutdown may be canceled by the session deadline.
            # Always reap both process groups before releasing device leases and sockets.
            cleanup = asyncio.create_task(self._cleanup())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
        if failures:
            raise RuntimeError("; ".join(failures))

    async def _cleanup(self):
        tasks = [*self.tasks, *self.feeds.values()]
        if self.browser:
            tasks.extend(
                task
                for name in ("task", "health_task")
                if (task := getattr(self.browser, name, None)) is not None
            )
        for task in tasks:
            task.cancel()
        if hasattr(self, "media_events"):
            self.media_events.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        children = [self.browser.child] if self.browser else []
        if self.media:
            children.append(self.media)
        try:
            results = await asyncio.gather(
                *(child.stop() for child in children), return_exceptions=True
            )
        finally:
            if self.lease:
                self.lease.close()
                self.lease = None
            if self.socket_dir:
                self.socket_dir.cleanup()
        for result in results:
            if isinstance(result, BaseException):
                raise result
