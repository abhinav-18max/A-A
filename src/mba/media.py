"""Portable fixture/recording utilities retained for compatibility.

Production media execution lives in the Rust media-core binary.
"""

from __future__ import annotations

import asyncio
import math
import queue
import struct
import threading
import wave
import zlib


def rgb_png(data: bytes, width: int, height: int) -> bytes:
    def chunk(tag, body):
        return struct.pack("!I", len(body)) + tag + body + struct.pack("!I", zlib.crc32(tag + body))

    rows = b"".join(b"\0" + data[y * width * 3 : (y + 1) * width * 3] for y in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows, 3))
        + chunk(b"IEND", b"")
    )


class PCMRecorder:
    def __init__(self, engine, name: str, channels: int):
        self.engine, self.name, self.channels = engine, name, channels
        self.queue = queue.Queue(maxsize=200)  # Four seconds at normal 20 ms chunking.
        self.thread = threading.Thread(target=self._run, name=f"record-{name}", daemon=True)
        self.error = None
        self.thread.start()

    def push(self, data: bytes, pts_us: int):
        try:
            self.queue.put_nowait((data, pts_us))
        except queue.Full:
            self.error = f"{self.name}: recording queue overflow"
            self.engine.error = self.error

    def _run(self):
        output = None
        path = None
        index = size = start = 0
        bytes_per_second = 48000 * self.channels * 2
        limit = bytes_per_second * 10
        try:
            while True:
                item = self.queue.get()
                if item is None:
                    break
                data, pts = item
                offset = 0
                while offset < len(data):
                    if output is None:
                        path = (
                            self.engine.evidence.root
                            / "recordings"
                            / f"{self.name}-{index:05d}.wav"
                        )
                        output = wave.open(str(path), "wb")
                        output.setparams((self.channels, 2, 48000, 0, "NONE", "not compressed"))
                        start, size = pts + offset * 1_000_000 // bytes_per_second, 0
                    part = data[offset : offset + limit - size]
                    output.writeframesraw(part)
                    offset += len(part)
                    size += len(part)
                    if size == limit:
                        output.close()
                        self.engine.publish_artifact(
                            path, "audio.segment", start, start + 10_000_000, stream=self.name
                        )
                        output = None
                        index += 1
            if output:
                output.close()
                self.engine.publish_artifact(
                    path,
                    "audio.segment",
                    start,
                    start + size * 1_000_000 // bytes_per_second,
                    stream=self.name,
                )
        except Exception as exc:
            self.error = self.engine.error = f"{self.name}: {exc}"
            if output:
                output.close()

    async def stop(self):
        await asyncio.to_thread(self.queue.put, None, True, 2)
        await asyncio.to_thread(self.thread.join, 3)
        if self.thread.is_alive():
            raise RuntimeError("audio recorder failed to finalize")


def sine_pcm(duration_s: float = 1, frequency: float = 440) -> bytes:
    return b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * frequency * i / 48000)))
        for i in range(int(duration_s * 48000))
    )
