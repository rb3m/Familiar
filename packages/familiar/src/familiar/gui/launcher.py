"""Open the launcher in an app-mode browser window when available."""

import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path


def open_window(url: str) -> None:
    candidates = [
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
        "brave-browser",
        "microsoft-edge",
    ]
    if sys.platform == "darwin":
        candidates.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    for candidate in candidates:
        executable = shutil.which(candidate)
        if executable is None and Path(candidate).is_absolute() and Path(candidate).is_file():
            executable = candidate
        if executable is None:
            continue
        try:
            subprocess.Popen([executable, f"--app={url}"], start_new_session=True)
            return
        except OSError:
            continue
    _open_fallback(url)


def _open_fallback(url: str) -> None:
    """Firefox has no app mode; a separate window is the closest thing to one."""
    firefox = shutil.which("firefox")
    if firefox is None:
        webbrowser.open(url)
        return
    try:
        subprocess.Popen([firefox, "--new-window", url], start_new_session=True)
    except OSError:
        webbrowser.open(url)
