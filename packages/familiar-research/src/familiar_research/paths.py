"""Shared private topic paths and atomic JSON persistence."""

import json
import os
import tempfile
from pathlib import Path


def topics_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "familiar" / "topics"


def state_path(topic_id: str, *, announced: bool = False) -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    suffix = {True: ".announced.json", False: ".json"}[announced]
    return base / "familiar" / "topics" / f"{topic_id}{suffix}"


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return value


def write_json(path: Path, value: dict) -> None:
    """Replace a private file without exposing a partial document to readers."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.parent.parent.chmod(0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
