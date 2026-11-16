"""Single-instance support: the running server records its URL in a private runtime file so a
second launch (clicking the icon again) opens a window to it instead of failing on the port."""

import json
import os
from pathlib import Path

import httpx

RUNTIME_FILE = (
    Path(os.environ.get("XDG_RUNTIME_DIR") or Path.home() / ".cache") / "familiar-gui.json"
)


def write(url: str, token: str, path: Path = RUNTIME_FILE) -> None:
    """Store the URL and token readable by this user only (the token guards the API)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"url": url, "token": token, "pid": os.getpid()}, handle)


def remove(path: Path = RUNTIME_FILE) -> None:
    path.unlink(missing_ok=True)


def running_url(path: Path = RUNTIME_FILE) -> str | None:
    """URL of a live server from a previous launch, or None (stale files are removed)."""
    try:
        info = json.loads(path.read_text())
        base = info["url"].split("/?", 1)[0]
        response = httpx.get(
            f"{base}/api/state", headers={"X-Familiar-Token": info["token"]}, timeout=2
        )
    except (OSError, ValueError, KeyError, httpx.HTTPError):
        remove(path)
        return None
    if response.status_code != 200:
        remove(path)
        return None
    return info["url"]
