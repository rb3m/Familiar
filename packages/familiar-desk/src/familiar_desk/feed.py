"""Single-writer atomic snapshots with independent source schedules."""

import asyncio
import fcntl
import json
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from familiar_desk.paths import feed_path, lock_path
from familiar_desk.sources import Source


def utc_now() -> datetime:
    return datetime.now(UTC)


class WriterBusyError(RuntimeError):
    """Another feed writer holds the runtime lock."""


class Feed:
    def __init__(
        self,
        sources: list[Source],
        *,
        clock: Callable[[], datetime] = utc_now,
        path: Path | None = None,
        lock: Path | None = None,
    ) -> None:
        self.sources = sources
        self.clock = clock
        self.path = path if path is not None else feed_path()
        self.lock = lock if lock is not None else lock_path()
        self.envelope = {
            "schema_version": 1,
            "instance_id": str(uuid4()),
            "sequence": 0,
            "generated_at": None,
            "sources": {},
            "events": [],
        }
        self._lock_file = None

    def acquire(self) -> None:
        handle = self.lock.open("a")
        os.fchmod(handle.fileno(), 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise WriterBusyError("Desk feed writer is already running (lock is held)") from exc
        self._lock_file = handle

    def close(self) -> None:
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None

    async def sample(self, source: Source) -> None:
        now = self.clock()
        previous = self.envelope["sources"].get(source.name, {})
        entry = {
            "observed_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=source.interval_s)).isoformat(),
            "status": "ok",
            "error": None,
            "data": {},
        }
        try:
            entry["data"], events = await source.sample(now)
            self.envelope["events"] = (self.envelope["events"] + events)[-50:]
        except Exception as exc:
            entry.update(status="error", error=str(exc), data=previous.get("data", {}))
        self.envelope["sources"][source.name] = entry
        self.write()

    def write(self) -> None:
        self.envelope["sequence"] += 1
        self.envelope["generated_at"] = self.clock().isoformat()
        fd, name = tempfile.mkstemp(prefix=".desk-feed-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(self.envelope, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)

    async def run(self, stop: asyncio.Event, *, once: bool = False) -> None:
        self.acquire()
        try:
            if once:
                await asyncio.gather(*(self.sample(source) for source in self.sources))
                return

            async def schedule(source: Source) -> None:
                while not stop.is_set():
                    await self.sample(source)
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=source.interval_s)
                    except TimeoutError:
                        continue

            async with asyncio.TaskGroup() as group:
                for source in self.sources:
                    group.create_task(schedule(source))
        finally:
            self.close()
