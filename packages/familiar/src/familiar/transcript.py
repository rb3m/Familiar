"""Flush each transcript event to a lazily created session JSONL file."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from familiar.interfaces import Message


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

    def dialogue(self, *, factual: bool = False) -> list[Message]:
        """Read actual user/assistant lines, excluding event prompts and diagnostics."""
        if not self.path.exists():
            return []
        messages = []
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                event = json.loads(line)
                if event["kind"] not in {"user", "assistant", "banter"} or not event.get("text"):
                    continue
                text = event["text"]
                if factual and (
                    event["kind"] == "banter"
                    or event.get("source") == "persona_claim"
                    or event.get("heard") is False
                    or event.get("interrupted")
                ):
                    text = "[Persona claim or interrupted line omitted from factual memory]"
                messages.append(
                    Message({"banter": "assistant"}.get(event["kind"], event["kind"]), text)
                )
        return messages
