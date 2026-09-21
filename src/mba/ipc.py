"""Private subprocess RPC transport. Never mutates the application's environment."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from pathlib import Path

import grpc

from mba.protocol import AdapterError


class Child:
    def __init__(self, socket: Path):
        self.socket = socket
        self.process = self.channel = None

    async def start(self, argv: list[str], env: dict[str, str], log: Path):
        if not shutil.which(argv[0]):
            raise AdapterError("runtime_missing", f"Executable not found: {argv[0]}", 503)
        with log.open("wb") as output:
            self.process = await asyncio.create_subprocess_exec(
                *argv,
                str(self.socket),
                env={**os.environ, **env},
                stdout=output,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        self.channel = grpc.aio.insecure_channel(
            f"unix:{self.socket}",
            options=[
                ("grpc.max_receive_message_length", 8 * 1024 * 1024),
                # UDS paths are not valid HTTP/2 authorities; Rust's h2 rejects them.
                ("grpc.default_authority", "localhost"),
            ],
        )
        ready = asyncio.create_task(self.channel.channel_ready())
        exited = asyncio.create_task(self.process.wait())
        try:
            done, _ = await asyncio.wait(
                [ready, exited], timeout=30, return_when=asyncio.FIRST_COMPLETED
            )
            if ready not in done:
                raise AdapterError(
                    "runtime_start_failed", f"Worker failed to start; see {log}", 503
                )
        finally:
            for task in (ready, exited):
                task.cancel()
            await asyncio.gather(ready, exited, return_exceptions=True)

    def check(self):
        if self.process and self.process.returncode is not None:
            raise AdapterError(
                "runtime_exited", f"Worker exited with {self.process.returncode}", 503
            )

    async def stop(self):
        if self.channel:
            await self.channel.close()
        if self.process:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except TimeoutError:
                pass
            # Also clean descendants if the worker exited before its children.
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await self.process.wait()
        self.socket.unlink(missing_ok=True)
