"""Private runtime paths for the desk feed."""

import os
import tempfile
from pathlib import Path


def feed_dir() -> Path:
    path = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()) / "familiar"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def feed_path() -> Path:
    return feed_dir() / "desk-feed.json"


def lock_path() -> Path:
    return feed_dir() / "desk-feed.lock"
