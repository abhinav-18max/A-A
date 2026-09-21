"""Run against `mba serve` on a prepared Linux host (or an SSH tunnel)."""

import argparse
import asyncio
import os
from pathlib import Path

from mba.protocol import Action, AudioTrack, SessionConfig, VideoTrack
from mba.sdk import Client


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8090")
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--video", type=Path)
    args = parser.parse_args()
    async with Client(args.url, os.environ["MBA_TOKEN"]) as client:
        session = await client.start_session(SessionConfig())

        async def observe():
            async for event in session.events():
                print(event.model_dump_json())

        observer = asyncio.create_task(observe())
        try:
            await (await session.send_text("Hello from the adapter")).wait()
            tracks = [
                AudioTrack(source={"kind": "asset", "asset_id": await session.upload(args.audio)})
            ]
            if args.video:
                tracks.append(
                    VideoTrack(
                        source={"kind": "asset", "asset_id": await session.upload(args.video)}
                    )
                )
            action = await session.submit(Action(tracks=tracks))
            await action.wait()
            print(await session.checkpoint())
        finally:
            print(await session.stop())
            await observer


if __name__ == "__main__":
    asyncio.run(main())
