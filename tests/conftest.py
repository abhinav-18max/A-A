from __future__ import annotations

import asyncio

import pytest

from mba.protocol import SessionConfig
from mba.session import Session


class FakeRuntime:
    """Explicit test double. Never selected by worker configuration or CLI."""

    def __init__(self, streams=None):
        self.streams = streams
        self.plays = []
        self.cancelled = []
        self.released = []
        self.stopped = False
        self.prepare_gate = None
        self.play_gate = None
        self.fail_play = False
        self.health_error = False
        self.play_started = asyncio.Event()

    async def start(self, config, evidence):
        self.evidence = evidence

    async def prepare(self, action):
        if self.prepare_gate:
            await self.prepare_gate.wait()

    async def play(self, action_id, track, start_us):
        await asyncio.sleep(max(0, start_us - self.evidence.clock.now_us()) / 1e6)
        self.plays.append((action_id, track.kind, self.evidence.clock.now_us()))
        self.play_started.set()
        if self.fail_play:
            raise RuntimeError("injected device failure")
        if self.play_gate:
            await self.play_gate.wait()

    async def cancel(self, action_id):
        self.cancelled.append(action_id)

    async def release(self, action_id):
        self.released.append(action_id)

    def check_health(self):
        if self.health_error:
            raise RuntimeError("lost device")

    async def stop(self, tail_s):
        self.stopped = True


@pytest.fixture
async def session(tmp_path):
    runtime = FakeRuntime()
    session = Session(
        "test-session", SessionConfig(capture_tail_s=0, min_free_disk_mb=1), tmp_path, runtime
    )
    await session.start()
    yield session
    await session.stop()


@pytest.fixture
def runtime_factory():
    return FakeRuntime
