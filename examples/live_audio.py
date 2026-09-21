import asyncio
import os

from mba.media import sine_pcm
from mba.protocol import Action, AudioTrack, StreamConfig
from mba.sdk import Client


async def main():
    async with Client("http://127.0.0.1:8090", os.environ["MBA_TOKEN"]) as client:
        session = await client.start_session()
        try:
            stream = await session.create_stream(StreamConfig(kind="audio", format="S16LE"))

            async def chunks():
                data = sine_pcm(3, 440)
                for index in range(0, len(data), 1920):
                    yield data[index : index + 1920]

            # Start producer and action together: the bounded stream intentionally backpressures.
            producer = asyncio.create_task(stream.feed(chunks()))
            action = await session.submit(Action(tracks=[AudioTrack(source=stream.source)]))
            await asyncio.gather(producer, action.wait())
        finally:
            await session.stop()


if __name__ == "__main__":
    asyncio.run(main())
