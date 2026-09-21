import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from mba.runtime_v2 import NativeRuntime


@pytest.mark.parametrize("phase", ["tail", "browser"])
async def test_canceled_shutdown_releases_every_resource(phase):
    entered = asyncio.Event()

    async def stop_browser():
        entered.set()
        await asyncio.sleep(60)

    runtime = NativeRuntime()
    child = SimpleNamespace(stop=AsyncMock())
    runtime.browser = SimpleNamespace(stop=stop_browser, child=child)
    runtime.media = SimpleNamespace(stop=AsyncMock())
    lease = runtime.lease = Mock()
    sockets = runtime.socket_dir = Mock()
    task = asyncio.create_task(runtime.stop(60 if phase == "tail" else 0))
    if phase == "browser":
        await asyncio.wait_for(entered.wait(), 1)
    else:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    child.stop.assert_awaited_once()
    runtime.media.stop.assert_awaited_once()
    lease.close.assert_called_once()
    sockets.cleanup.assert_called_once()
