from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Protocol

from mba.evidence import Clock, Evidence
from mba.protocol import (
    TERMINAL_ACTIONS,
    Action,
    ActionState,
    ActionStatus,
    AdapterError,
    SessionConfig,
    Track,
)


def error_message(exc):
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(error_message(item) for item in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"


class Runtime(Protocol):
    async def start(self, config: SessionConfig, evidence: Evidence): ...
    async def prepare(self, action: Action): ...
    async def play(self, action_id: str, track: Track, start_us: int): ...
    async def cancel(self, action_id: str): ...
    async def release(self, action_id: str): ...
    async def stop(self, tail_s: float): ...


class Session:
    def __init__(self, session_id: str, config: SessionConfig, root: Path, runtime: Runtime):
        self.id, self.config, self.runtime = session_id, config, runtime
        self.clock = Clock()
        self.evidence = Evidence(root, session_id, self.clock, persistent=config.persistent_events)
        self.state = "allocating"
        self.actions: dict[str, Action] = {}
        self.statuses: dict[str, ActionStatus] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.owners: dict[str, str] = {}
        self.lock = asyncio.Lock()
        self.stop_lock = asyncio.Lock()
        self.watchdog: asyncio.Task | None = None
        self.failure: str | None = None

    async def transition(self, state: str, **data):
        self.state = state
        self.evidence.update(state=state, **data)
        await self.evidence.emit("session.state", {"state": state, **data})

    async def start(self):
        try:
            self.check_disk()
            self.evidence.update(config=self.config.model_dump(mode="json"))
            async with asyncio.timeout(self.config.startup_timeout_s):
                self.runtime.on_failure = lambda reason: self.stop(reason=reason)
                await self.runtime.start(self.config, self.evidence)
            await self.transition("ready")
            self.watchdog = asyncio.create_task(self._watch(), name=f"watch-{self.id}")
        except BaseException as exc:
            await self.stop(reason=f"startup_failed: {type(exc).__name__}: {exc}")
            raise

    def check_disk(self):
        if shutil.disk_usage(self.evidence.root).free < self.config.min_free_disk_mb * 1024**2:
            raise AdapterError("storage_full", "insufficient artifact disk space", 507)

    async def _watch(self):
        try:
            while self.state == "ready":
                await asyncio.sleep(1)
                self.check_disk()
                health = getattr(self.runtime, "check_health", None)
                if health:
                    health()
                if self.clock.now_us() > self.config.max_duration_s * 1_000_000:
                    await self.stop()
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self.stop(reason=f"runtime_failed: {exc}")

    async def submit(self, action: Action) -> ActionStatus:
        async with self.lock:
            if action.action_id in self.actions:
                if action != self.actions[action.action_id]:
                    raise AdapterError(
                        "action_conflict", "action ID reused with a different request"
                    )
                return self.statuses[action.action_id]
            if self.state != "ready":
                raise AdapterError("session_not_ready", f"session is {self.state}")
            if self.config.browser_control == "native":
                if any(t.kind == "text" for t in action.tracks):
                    raise AdapterError(
                        "browser_control_external", "Submit text through native Playwright"
                    )
                caps = await self.runtime.browser.command("capabilities")
                if not caps["target_ready"]:
                    raise AdapterError("target_not_ready", "Register the native target page first")
            for track in action.tracks:
                if (track.kind == "audio" and self.config.mode not in {"audio", "multimodal"}) or (
                    track.kind == "video" and not self.config.camera_enabled
                ):
                    raise AdapterError(
                        "unsupported_modality", "Requested modality is disabled", 422
                    )
                if not getattr(self.config.binding, f"supports_{track.kind}"):
                    raise AdapterError(
                        "unsupported_modality", f"binding does not support {track.kind}", 422
                    )
            conflicts = {self.owners[t.kind] for t in action.tracks if t.kind in self.owners}
            if conflicts and not action.replace:
                raise AdapterError("resource_busy", "requested modality is in use")
            # Reserve the entire bundle before starting any work. No await while ownership changes.
            for old in conflicts:
                await self._cancel(old)
            for track in action.tracks:
                self.owners[track.kind] = action.action_id
            self.actions[action.action_id] = action
            status = ActionStatus(
                action_id=action.action_id,
                state=ActionState.ACCEPTED,
                accepted_at_us=self.clock.now_us(),
            )
            self.statuses[action.action_id] = status
            await self.evidence.emit("action.status", status.model_dump(mode="json"))
            self.tasks[action.action_id] = asyncio.create_task(self._run(action))
            return status.model_copy()

    async def _status(self, action_id: str, state: ActionState, error: str | None = None):
        status = self.statuses[action_id]
        if status.state in TERMINAL_ACTIONS:
            return
        status.state = state
        if state == ActionState.STARTED:
            status.started_at_us = self.clock.now_us()
        if state in TERMINAL_ACTIONS:
            status.ended_at_us = self.clock.now_us()
            status.error = error
        await self.evidence.emit("action.status", status.model_dump(mode="json"))

    async def _abort_runtime(self, action_id):
        try:
            async with asyncio.timeout(2):
                await self.runtime.cancel(action_id)
        except Exception as exc:
            await self.evidence.emit(
                "action.cleanup_failed", {"action_id": action_id, "reason": str(exc)}
            )

    async def _run(self, action: Action):
        try:
            async with asyncio.timeout(self.config.action_timeout_s):
                await self.runtime.prepare(action)
                now = self.clock.now_us()
                if action.start_at_us is not None and action.start_at_us < now:
                    raise AdapterError(
                        "deadline_missed", "tracks were not ready by scheduled start"
                    )
                start = action.start_at_us if action.start_at_us is not None else now
                await asyncio.sleep(max(0, start - self.clock.now_us()) / 1_000_000)
                await self._status(action.action_id, ActionState.STARTED)
                async with asyncio.TaskGroup() as group:
                    for track in action.tracks:
                        group.create_task(
                            self.runtime.play(action.action_id, track, start + track.offset_us)
                        )
                await self._status(action.action_id, ActionState.COMPLETED)
        except asyncio.CancelledError:
            await self._abort_runtime(action.action_id)
            await self._status(action.action_id, ActionState.CANCELLED)
        except Exception as exc:
            await self._abort_runtime(action.action_id)
            await self._status(action.action_id, ActionState.FAILED, error_message(exc))
        finally:
            try:
                await self.runtime.release(action.action_id)
            except Exception as exc:
                await self.evidence.emit(
                    "action.cleanup_failed", {"action_id": action.action_id, "reason": str(exc)}
                )
            finally:
                for kind, owner in list(self.owners.items()):
                    if owner == action.action_id:
                        del self.owners[kind]

    async def _cancel(self, action_id: str):
        if action_id not in self.tasks:
            raise AdapterError("action_not_found", "unknown action", 404)
        task = self.tasks[action_id]
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # Handles cancellation before coroutine first ran.
        await self._status(action_id, ActionState.CANCELLED)
        for kind, owner in list(self.owners.items()):
            if owner == action_id:
                del self.owners[kind]
        return self.statuses[action_id]

    async def cancel(self, action_id: str):
        async with self.lock:
            if (
                action_id in self.statuses
                and self.statuses[action_id].state not in TERMINAL_ACTIONS
            ):
                await self.evidence.emit("action.cancel_requested", {"action_id": action_id})
            return await self._cancel(action_id)

    async def stop(self, reason: str | None = None):
        async with self.stop_lock:
            if self.state in {"closed", "failed"}:
                return self.evidence.manifest
            self.failure = reason
            async with self.lock:
                await self.transition("draining")
                if self.watchdog and self.watchdog is not asyncio.current_task():
                    self.watchdog.cancel()
                for action_id in list(self.tasks):
                    await self._cancel(action_id)
            try:
                async with asyncio.timeout(15):
                    await self.runtime.stop(0 if reason else self.config.capture_tail_s)
            except Exception as exc:
                self.failure = self.failure or f"shutdown_failed: {exc}"
            self.evidence.update(
                actions={
                    aid: status.model_dump(mode="json") for aid, status in self.statuses.items()
                }
            )
            await self.transition(
                "failed" if self.failure else "closed",
                error=self.failure,
                incomplete=bool(self.failure),
            )
            await self.evidence.close()
            return self.evidence.manifest
