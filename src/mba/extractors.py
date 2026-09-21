"""Provider-neutral enrichment. No extractor participates in capture scheduling."""

from __future__ import annotations

import asyncio
from typing import Protocol

from mba.evidence import Evidence
from mba.protocol import Event


class Extractor(Protocol):
    name: str
    version: str
    accepts: set[str]

    async def extract(self, event: Event, evidence: Evidence) -> list[tuple[str, dict]]: ...


class ExtractionRunner:
    def __init__(self, extractor: Extractor, evidence: Evidence, capacity: int = 8):
        self.extractor, self.evidence = extractor, evidence
        self.queue = asyncio.Queue(maxsize=capacity)

    async def offer(self, event: Event):
        if event.type not in self.extractor.accepts:
            return
        if self.queue.full():
            dropped = self.queue.get_nowait()
            await self.evidence.emit(
                "extraction.gap",
                {
                    "processor": self.extractor.name,
                    "evidence_id": dropped.id,
                    "reason": "consumer_overloaded",
                },
            )
        self.queue.put_nowait(event)

    async def run(self):
        while True:
            event = await self.queue.get()
            try:
                async with asyncio.timeout(30):
                    observations = await self.extractor.extract(event, self.evidence)
                for event_type, data in observations:
                    await self.evidence.emit(
                        event_type,
                        {
                            **data,
                            "evidence_ids": [event.id],
                            "processor": self.extractor.name,
                            "processor_version": self.extractor.version,
                        },
                        event.observed_at_us,
                    )
            except Exception as exc:
                await self.evidence.emit(
                    "extraction.failed",
                    {"processor": self.extractor.name, "evidence_id": event.id, "reason": str(exc)},
                )


class DeterministicTestExtractor:
    """Contract fixture, never represented as real transcription or vision."""

    name = "test-fixture"
    version = "1"
    accepts = {"audio.segment.available", "frame.available"}

    async def extract(self, event, evidence):
        return [("extraction.test", {"fixture": True, "input_type": event.type})]
