from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from mba.protocol import AdapterError, Event, new_id


class Clock:
    def __init__(self):
        self.origin_ns = time.monotonic_ns()
        self.utc_origin = datetime.now(UTC).isoformat()

    def now_us(self) -> int:
        return (time.monotonic_ns() - self.origin_ns) // 1000


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as f:
        json.dump(value, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    temporary.replace(path)


class Evidence:
    def __init__(self, root: Path, session_id: str, clock: Clock, persistent: bool = True):
        self.persistent = persistent
        self.root, self.session_id, self.clock = root, session_id, clock
        root.mkdir(parents=True, exist_ok=True)
        (root / "assets").mkdir(exist_ok=True)
        (root / "recordings").mkdir(exist_ok=True)
        (root / "frames").mkdir(exist_ok=True)
        self.db = sqlite3.connect(root / "events.sqlite3" if persistent else ":memory:")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY, body TEXT NOT NULL)"
        )
        self.sequence = self.db.execute("SELECT coalesce(max(sequence),0) FROM events").fetchone()[
            0
        ]
        self.changed = asyncio.Condition()
        self.closed = False
        self.manifest = {
            "session_id": session_id,
            "schema_version": 1,
            "utc_origin": clock.utc_origin,
            "state": "allocating",
            "artifacts": [],
        }
        atomic_json(root / "manifest.json", self.manifest)

    async def emit(
        self, event_type: str, data: dict | None = None, observed_at_us: int | None = None
    ):
        async with self.changed:
            if self.closed:
                return None
            self.sequence += 1
            event = Event(
                session_id=self.session_id,
                sequence=self.sequence,
                type=event_type,
                observed_at_us=self.clock.now_us() if observed_at_us is None else observed_at_us,
                emitted_at_us=self.clock.now_us(),
                data=data or {},
            )
            self.db.execute(
                "INSERT INTO events VALUES (?,?)", (event.sequence, event.model_dump_json())
            )
            self.db.commit()
            if not self.persistent:
                self.db.execute("DELETE FROM events WHERE sequence <= ?", (self.sequence - 4096,))
                self.db.commit()
            self.changed.notify_all()
            return event

    def read(self, after: int, limit: int = 256) -> list[Event]:
        return [
            Event.model_validate_json(row[0])
            for row in self.db.execute(
                "SELECT body FROM events WHERE sequence > ? ORDER BY sequence LIMIT ?",
                (after, limit),
            )
        ]

    async def events(self, after: int = 0):
        while True:
            async with self.changed:
                batch = self.read(after)
                if batch and after and batch[0].sequence > after + 1:
                    raise AdapterError(
                        "event_history_expired", "Subscriber fell behind bounded event history"
                    )
                if not batch:
                    if self.closed:
                        return
                    await self.changed.wait()
                    continue
            for event in batch:
                after = event.sequence
                yield event

    def update(self, **changes):
        self.manifest.update(changes)
        atomic_json(self.root / "manifest.json", self.manifest)

    async def artifact(self, path: Path, kind: str, start_us: int, end_us: int, **metadata):
        info = {
            "path": str(path.relative_to(self.root)),
            "kind": kind,
            "start_us": start_us,
            "end_us": end_us,
            "complete": True,
            **metadata,
        }
        self.manifest["artifacts"].append(info)
        self.update()
        await self.emit(f"{kind}.available", info, start_us)

    async def close(self):
        async with self.changed:
            self.closed = True
            self.db.execute("PRAGMA wal_checkpoint(FULL)")
            self.changed.notify_all()
        # Keep the DB available for final event replay until process exit.

    def resolve(self, relative: str) -> Path:
        candidate = (self.root / relative).resolve()
        if not candidate.is_relative_to(self.root.resolve()) or not candidate.is_file():
            raise AdapterError("artifact_not_found", "artifact does not exist", 404)
        return candidate

    def asset(self, asset_id: str) -> Path:
        return self.resolve("assets/" + asset_id)

    async def upload(self, chunks, limit: int = 512 * 1024 * 1024) -> str:
        asset_id = new_id()
        path = self.root / "assets" / asset_id
        total = 0
        try:
            with path.open("xb") as output:
                async for chunk in chunks:
                    total += len(chunk)
                    if total > limit:
                        raise AdapterError("asset_too_large", "asset exceeds 512 MiB", 413)
                    output.write(chunk)
            if not total:
                raise AdapterError("empty_asset", "asset cannot be empty", 422)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return asset_id
