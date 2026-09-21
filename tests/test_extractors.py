import asyncio

from mba.evidence import Clock, Evidence
from mba.extractors import DeterministicTestExtractor, ExtractionRunner


async def test_enrichment_preserves_evidence_time_and_provenance(tmp_path):
    evidence = Evidence(tmp_path, "s", Clock())
    event = await evidence.emit("frame.available", {"path": "frame.png"}, 123)
    runner = ExtractionRunner(DeterministicTestExtractor(), evidence)
    await runner.offer(event)
    task = asyncio.create_task(runner.run())
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    result = evidence.read(1)[0]
    assert result.observed_at_us == 123
    assert result.data["evidence_ids"] == [event.id]
    assert result.data["fixture"] is True
    await evidence.close()


async def test_extractor_overload_is_observable(tmp_path):
    evidence = Evidence(tmp_path, "s", Clock())
    runner = ExtractionRunner(DeterministicTestExtractor(), evidence, capacity=1)
    a = await evidence.emit("frame.available")
    b = await evidence.emit("frame.available")
    await runner.offer(a)
    await runner.offer(b)
    assert runner.queue.qsize() == 1
    assert any(e.type == "extraction.gap" for e in evidence.read(0))
    await evidence.close()
