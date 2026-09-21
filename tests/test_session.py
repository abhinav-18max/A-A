import asyncio

import pytest

from mba.protocol import Action, ActionState, AdapterError, AudioTrack, TextTrack, VideoTrack


def text(action_id="hello", content="hello"):
    return Action(action_id=action_id, tracks=[TextTrack(content=content)])


async def test_duplicate_request_executes_once_and_conflicting_reuse_fails(session):
    await session.submit(text())
    await session.tasks["hello"]
    again = await session.submit(text())
    assert again.state == ActionState.COMPLETED
    assert len(session.runtime.plays) == 1
    with pytest.raises(AdapterError, match="different request"):
        await session.submit(text(content="different"))


async def test_busy_bundle_does_not_partially_reserve_modalities(session):
    session.runtime.play_gate = asyncio.Event()
    await session.submit(text())
    bundle = Action(
        tracks=[
            TextTrack(content="busy"),
            AudioTrack(source={"kind": "asset", "asset_id": "a" * 32}),
        ]
    )
    with pytest.raises(AdapterError, match="in use"):
        await session.submit(bundle)
    assert "audio" not in session.owners


async def test_cancel_before_task_runs_releases_resources(session):
    await session.submit(text())
    result = await session.cancel("hello")
    assert result.state == ActionState.CANCELLED
    assert not session.owners
    assert not session.runtime.plays


async def test_cancel_during_preparation_is_terminal_once(session):
    session.runtime.prepare_gate = asyncio.Event()
    await session.submit(text())
    await asyncio.sleep(0)
    await session.cancel("hello")
    await session.cancel("hello")
    states = [e.data["state"] for e in session.evidence.read(0) if e.type == "action.status"]
    assert states == ["accepted", "cancelled"]
    assert "hello" in session.runtime.released


async def test_replace_waits_for_cancel_and_owns_resource(session):
    session.runtime.play_gate = asyncio.Event()
    await session.submit(text())
    await session.runtime.play_started.wait()
    await session.submit(
        Action(action_id="replacement", tracks=[TextTrack(content="new")], replace=True)
    )
    assert session.statuses["hello"].state == ActionState.CANCELLED
    assert session.owners == {"text": "replacement"}


async def test_synchronized_bundle_tracks_share_start_with_offset(session):
    action = Action(
        action_id="av",
        start_at_us=session.clock.now_us() + 40_000,
        tracks=[
            AudioTrack(source={"kind": "asset", "asset_id": "a" * 32}),
            VideoTrack(source={"kind": "asset", "asset_id": "b" * 32}, offset_us=40_000),
        ],
    )
    await session.submit(action)
    await session.tasks["av"]
    times = {kind: when for _, kind, when in session.runtime.plays}
    assert times["audio"] >= action.start_at_us
    assert times["video"] - times["audio"] >= 25_000


async def test_missed_deadline_never_plays(session):
    await session.submit(
        Action(action_id="late", start_at_us=0, tracks=[TextTrack(content="late")])
    )
    await session.tasks["late"]
    assert session.statuses["late"].state == ActionState.FAILED
    assert not session.runtime.plays
    assert not session.owners


async def test_track_failure_cancels_bundle_and_releases(session):
    session.runtime.fail_play = True
    await session.submit(text())
    await session.tasks["hello"]
    assert session.statuses["hello"].state == ActionState.FAILED
    assert session.runtime.cancelled == ["hello"]
    assert not session.owners


async def test_shutdown_cancels_active_action_and_rejects_new_work(session):
    session.runtime.play_gate = asyncio.Event()
    await session.submit(text())
    await session.runtime.play_started.wait()
    result = await session.stop()
    assert result["state"] == "closed"
    assert session.runtime.stopped
    assert session.statuses["hello"].state == ActionState.CANCELLED
    assert await session.stop() == result
    with pytest.raises(AdapterError, match="closed"):
        await session.submit(text("new"))


async def test_action_timeout_is_failure_not_completion(session):
    session.config.action_timeout_s = 0.01
    session.runtime.play_gate = asyncio.Event()
    await session.submit(text())
    await session.tasks["hello"]
    assert session.statuses["hello"].state == ActionState.FAILED
    assert session.runtime.cancelled
