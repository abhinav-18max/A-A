import asyncio
from types import SimpleNamespace

import pytest

from mba.connectors import Bridge, Capabilities, Output, Routes, Subscription
from mba.protocol import AdapterError


class Connector:
    capabilities = Capabilities(text_output=True, interruptions=True)
    closed = False
    interrupted = []

    async def connect(self):
        pass

    async def close(self):
        self.closed = True

    async def interrupt(self, response_id):
        self.interrupted.append(response_id)

    async def outputs(self):
        yield Output("old", "text", "stale")
        yield Output("new", "text", "hello")


async def test_bridge_rejects_unsupported_route():
    with pytest.raises(AdapterError, match="unsupported"):
        async with Bridge(None, Connector(), Routes(target_audio=True)):
            pass


async def test_bridge_discards_canceled_response_without_policy():
    sent = []

    class Action:
        async def wait(self):
            pass

    class Text:
        async def send(self, value):
            sent.append(value)
            return Action()

    class Session:
        text = Text()

    connector = Connector()
    bridge = Bridge(Session(), connector, Routes(output_text=True))
    await bridge.interrupt("old")
    async with bridge:
        await bridge.wait()
    assert sent == ["hello"]
    assert connector.closed


async def test_callback_failure_is_observable_and_cancels_producer():
    ended = asyncio.Event()

    async def source():
        try:
            yield 1
            await asyncio.sleep(60)
        finally:
            ended.set()

    async def callback(_):
        raise ValueError("callback failed")

    with pytest.raises(ExceptionGroup, match="TaskGroup"):
        async with Subscription(source(), callback) as sub:
            await sub.wait()
    assert ended.is_set()


@pytest.mark.parametrize("phase", ["write", "drain"])
async def test_interrupt_in_flight_media_keeps_bridge_alive(phase):
    entered, canceled = asyncio.Event(), asyncio.Event()
    sent = []

    class Stream:
        async def __aenter__(self):
            return self

        async def write(self, chunk):
            if phase == "write":
                entered.set()
                await canceled.wait()
                raise asyncio.CancelledError()

        async def __aexit__(self, *_):
            if phase == "drain":
                entered.set()
                await canceled.wait()

        async def cancel(self):
            canceled.set()

    stream = Stream()

    class MediaConnector(Connector):
        capabilities = Capabilities(audio_output=True, text_output=True, interruptions=True)

        async def outputs(self):
            yield Output("old", "audio", SimpleNamespace(format=None))
            yield Output("old", "end")
            yield Output("new", "text", "next response")

    class Text:
        async def send(self, value):
            sent.append(value)
            return SimpleNamespace(wait=lambda: asyncio.sleep(0))

    session = SimpleNamespace(audio=SimpleNamespace(open_input=lambda _: stream), text=Text())
    async with Bridge(
        session, MediaConnector(), Routes(output_audio=True, output_text=True)
    ) as bridge:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(bridge.interrupt("old"), 1)
        assert canceled.is_set()
        await asyncio.wait_for(bridge.wait(), 1)
        assert sent == ["next response"]
