"""Bounded, opaque duplex forwarding for native Playwright connections."""

import asyncio

from websockets.asyncio.client import connect


async def proxy_websocket(ws, endpoint, headers):
    async with connect(
        endpoint, additional_headers=headers, max_size=32 * 1024**2, max_queue=16
    ) as upstream:

        async def outgoing():
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    return
                await upstream.send(
                    message["bytes"] if message.get("bytes") is not None else message["text"]
                )

        async def incoming():
            async for message in upstream:
                if isinstance(message, bytes):
                    await ws.send_bytes(message)
                else:
                    await ws.send_text(message)

        tasks = [asyncio.create_task(outgoing()), asyncio.create_task(incoming())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
