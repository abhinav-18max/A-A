from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass

from mba.protocol import AdapterError, StreamConfig, new_id

# Wire frame: unsigned big-endian sequence and relative presentation timestamp, then media bytes.
HEADER = struct.Struct("!QQ")


@dataclass
class Packet:
    sequence: int
    pts_us: int
    data: bytes


class InputStream:
    def __init__(self, config: StreamConfig):
        self.id, self.config = new_id(), config
        self.queue: asyncio.Queue[Packet | None] = asyncio.Queue(
            10 if config.kind == "audio" else 3
        )
        self.last_sequence = -1
        self.last_pts = -1
        self.closed = False
        self.claimed = False
        self.connected = False
        self.dropped = 0
        self.ready = asyncio.Event()
        self.error: str | None = None

    async def put(self, wire: bytes):
        if self.closed:
            raise AdapterError("stream_closed", "stream has ended")
        expected = self.config.packet_bytes
        if len(wire) != HEADER.size + expected:
            raise AdapterError("invalid_frame", f"frame payload must be {expected} bytes", 422)
        sequence, pts = HEADER.unpack_from(wire)
        step = 20_000 if self.config.kind == "audio" else None
        if sequence != self.last_sequence + 1 or pts <= self.last_pts:
            raise AdapterError(
                "invalid_sequence", "sequence must be contiguous and timestamps increasing", 422
            )
        if sequence == 0 and pts != 0:
            raise AdapterError("invalid_timestamp", "first timestamp must be zero", 422)
        if step and pts != sequence * step:
            raise AdapterError("invalid_timestamp", "audio timestamps must advance by 20 ms", 422)
        if self.config.kind == "video" and pts != self.config.pts(sequence):
            raise AdapterError(
                "invalid_timestamp", "video timestamps must follow declared FPS", 422
            )
        if self.config.kind == "video" and self.queue.full():
            self.queue.get_nowait()
            self.dropped += 1
        await self.queue.put(Packet(sequence, pts, wire[HEADER.size :]))
        self.last_sequence, self.last_pts = sequence, pts
        self.ready.set()

    async def end(self):
        if not self.closed:
            self.closed = True
            try:
                await self.queue.put(None)
                self.ready.set()
            except BaseException:
                self.abort("producer interrupted while closing stream")
                raise

    def abort(self, reason: str):
        self.error = reason
        self.closed = True
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait(None)
        self.ready.set()

    async def packets(self):
        while True:
            packet = await self.queue.get()
            if packet is None:
                if self.error:
                    raise AdapterError("stream_aborted", self.error)
                return
            yield packet


class StreamRegistry:
    def __init__(self):
        self.streams: dict[str, InputStream] = {}

    def create(self, config: StreamConfig):
        if len(self.streams) >= 100:
            raise AdapterError("stream_limit", "maximum streams per session reached")
        stream = InputStream(config)
        self.streams[stream.id] = stream
        return stream

    def get(self, stream_id: str):
        try:
            return self.streams[stream_id]
        except KeyError:
            raise AdapterError("stream_not_found", "unknown stream", 404) from None

    def claim(self, stream_id: str, kind: str):
        stream = self.get(stream_id)
        if stream.claimed or stream.config.kind != kind:
            raise AdapterError("invalid_stream", "stream already used or wrong modality")
        stream.claimed = True
        return stream

    def abort_all(self, reason: str):
        for stream in self.streams.values():
            stream.abort(reason)
