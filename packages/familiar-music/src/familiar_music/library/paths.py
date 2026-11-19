"""Paths for music library roots and database."""

import os
import re
import shutil
import subprocess
from pathlib import Path


def _from_xdg_tool() -> Path | None:
    tool = shutil.which("xdg-user-dir")
    if not tool:
        return None
    try:
        proc = subprocess.run(
            [tool, "MUSIC"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        out = proc.stdout.strip()
        if proc.returncode == 0 and out:
            return Path(out).expanduser()
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def _from_user_dirs_file() -> Path | None:
    config_file = Path("~/.config/user-dirs.dirs").expanduser()
    if not config_file.is_file():
        return None
    try:
        lines = config_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        match = re.match(r'^\s*XDG_MUSIC_DIR\s*=\s*["\']?(.*?)["\']?\s*$', line)
        if not match:
            continue
        raw = match.group(1).replace("$HOME", str(Path.home()))
        return Path(raw).expanduser()
    return None


def xdg_music_dir() -> Path:
    """Detect the user Music directory via xdg-user-dir, user-dirs.dirs, or ~/Music."""
    from_tool = _from_xdg_tool()
    if from_tool is not None:
        return from_tool
    from_file = _from_user_dirs_file()
    if from_file is not None:
        return from_file
    return Path("~/Music").expanduser()


def library_roots(music_config: dict | None = None) -> list[Path]:
    """Library roots from [music] library_paths, falling back to XDG Music dir."""
    raw_paths = music_config.get("library_paths") if music_config else None
    if raw_paths:
        return [Path(p).expanduser().resolve() for p in raw_paths]
    return [xdg_music_dir().resolve()]


def database_path(music_config: dict | None = None) -> Path:
    """Path to the SQLite library database."""
    if music_config and music_config.get("library_db"):
        return Path(music_config["library_db"]).expanduser().resolve()
    state_root = Path(os.environ.get("XDG_STATE_HOME") or "~/.local/state").expanduser()
    return (state_root / "familiar" / "music" / "library.sqlite").resolve()
