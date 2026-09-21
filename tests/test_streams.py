import asyncio

import pytest

from mba.protocol import AdapterError, StreamConfig
from mba.streams import HEADER, InputStream, StreamRegistry


def audio_frame(index):
    return HEADER.pack(index, index * 20_000) + bytes(1920)


async def test_audio_backpressure_never_silently_drops_samples():
    stream = InputStream(StreamConfig(kind="audio", format="S16LE"))
    for i in range(10):
        await stream.put(audio_frame(i))
    pending = asyncio.create_task(stream.put(audio_frame(10)))
    await asyncio.sleep(0)
    assert not pending.done()
    assert stream.queue.get_nowait().sequence == 0
    await pending
    assert stream.dropped == 0


async def test_video_discards_stale_frames_with_accounting():
    stream = InputStream(StreamConfig(kind="video", format="YUY2"))
    for i in range(5):
        await stream.put(HEADER.pack(i, i * 1_000_000 // 30) + bytes(1280 * 720 * 2))
    assert stream.dropped == 2
    assert stream.queue.get_nowait().sequence == 2


async def test_invalid_packet_cannot_advance_sequence():
    stream = InputStream(StreamConfig(kind="audio", format="S16LE"))
    with pytest.raises(AdapterError):
        await stream.put(audio_frame(1))
    assert stream.last_sequence == -1
    await stream.put(audio_frame(0))
    with pytest.raises(AdapterError):
        await stream.put(audio_frame(0))


async def test_disconnect_is_failure_and_cannot_look_like_successful_eos():
    stream = InputStream(StreamConfig(kind="audio", format="S16LE"))
    await stream.put(audio_frame(0))
    stream.abort("network disconnected")
    with pytest.raises(AdapterError, match="network disconnected"):
        async for _ in stream.packets():
            pass


async def test_cancelled_eos_does_not_leave_a_reader_hanging():
    stream = InputStream(StreamConfig(kind="audio", format="S16LE"))
    for i in range(10):
        await stream.put(audio_frame(i))
    task = asyncio.create_task(stream.end())
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    with pytest.raises(AdapterError, match="closing stream"):
        async for _ in stream.packets():
            pass


def test_stream_cannot_be_claimed_twice_or_as_wrong_modality():
    registry = StreamRegistry()
    stream = registry.create(StreamConfig(kind="audio", format="S16LE"))
    with pytest.raises(AdapterError):
        registry.claim(stream.id, "video")
    registry.claim(stream.id, "audio")
    with pytest.raises(AdapterError):
        registry.claim(stream.id, "audio")


async def test_legacy_video_drops_are_resequenced_for_native_rpc():
    from mba.evidence import Clock
    from mba.runtime_v2 import NativeRuntime
    from mba.streams import Packet

    class Stream:
        id = "stream"
        config = StreamConfig(kind="video", format="YUY2")

        async def packets(self):
            yield Packet(2, 66666, b"first")
            yield Packet(5, 166666, b"second")

    captured, gaps = [], []

    class Stub:
        async def Input(self, packets):
            async for packet in packets:
                captured.append(packet)

    class Evidence:
        clock = Clock()

        async def emit(self, kind, payload):
            gaps.append(payload["dropped"])

    runtime = NativeRuntime()
    runtime.media_stub, runtime.evidence = Stub(), Evidence()
    await runtime._feed_legacy(Stream())
    assert [p.sequence for p in captured] == [0, 1, 2]
    assert [p.pts_us for p in captured[:2]] == [66666, 166666]
    assert captured[-1].end
    assert gaps == [2, 2]
