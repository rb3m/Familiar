"""Binary-offset journal tailing; historical lines rebuild state without speech."""

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class JournalRecord:
    data: dict
    historical: bool = False


class JournalTailer:
    def __init__(self, directory: Path | None, poll_interval_s: float = 0.5):
        self.directory = directory
        self.poll_interval_s = poll_interval_s
        self.path = None
        self.identity = None
        self.offset = 0
        self.pending = b""
        self.boundary = 0
        self.started = False
        self.closed = False

    def poll(self) -> list[JournalRecord]:
        try:
            return self._read()
        except OSError:
            return []

    def _read(self) -> list[JournalRecord]:
        if self.directory is None:
            return []
        path = max(self.directory.glob("Journal.*.log"), key=lambda p: p.name, default=None)
        if path is None:
            self.started = True
            return []
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        if path != self.path or identity != self.identity or stat.st_size < self.offset:
            self.path, self.identity = path, identity
            self.offset, self.pending = 0, b""
            self.boundary = stat.st_size if not self.started else 0
        self.started = True
        with path.open("rb") as stream:
            stream.seek(self.offset)
            chunk = stream.read()
        start = self.offset - len(self.pending)
        self.offset += len(chunk)
        lines = (self.pending + chunk).split(b"\n")
        self.pending = lines.pop()
        result = []
        for line in lines:
            record = self._decode(line, start < self.boundary)
            start += len(line) + 1
            if record is not None:
                result.append(record)
        return result

    @staticmethod
    def _decode(line: bytes, historical: bool) -> JournalRecord | None:
        try:
            data = json.loads(line.decode("utf-8-sig"))
        except (UnicodeError, ValueError):
            log.debug("Skipping undecodable Elite journal line")
            return None
        if not isinstance(data, dict) or not isinstance(data.get("event"), str):
            return None
        return JournalRecord(data, historical)

    async def __aiter__(self):
        pending = deque()
        while not self.closed:
            if pending:
                yield pending.popleft()
                continue
            pending.extend(await asyncio.to_thread(self.poll))
            await asyncio.sleep(self.poll_interval_s)

    async def aclose(self):
        self.closed = True
