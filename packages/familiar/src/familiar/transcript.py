"""Flush each transcript event to a lazily created session JSONL file."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


class TranscriptLog:
    def __init__(self, directory: Path, session_id: str | None = None) -> None:
        self.directory = Path(directory)
        identifier = (session_id or uuid4().hex)[:8]
        if not identifier or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for char in identifier
        ):
            raise ValueError("session_id must start with safe filename characters")
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        self.path = self.directory / f"{timestamp}-{identifier}.jsonl"

    def record(self, kind: str, **data: object) -> None:
        """Append an event and flush before returning."""
        event = {**data, "v": 1, "ts": datetime.now(UTC).isoformat(), "kind": kind}
        self.directory.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            handle.flush()
