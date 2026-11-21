"""Open the launcher in an app-mode browser window when available."""

import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

LINUX_BROWSERS = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "brave-browser",
    "microsoft-edge",
)
# macOS app bundles with an --app mode, opened through LaunchServices: running a bundle's
# binary directly can abort on the profile lock and never show a window.
MAC_BROWSERS = ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium")
MAC_APP_DIRS = (Path("/Applications"), Path.home() / "Applications")


def _spawn(argv: list[str]) -> bool:
    # Detached stdio: an inherited pipe would keep Familiar.app's shell script waiting.
    try:
        subprocess.Popen(
            argv,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return True


def _mac_installed(name: str) -> bool:
    return any((folder / f"{name}.app").is_dir() for folder in MAC_APP_DIRS)


def _app_commands(url: str) -> list[list[str]]:
    if sys.platform == "darwin":
        return [
            ["open", "-na", name, "--args", f"--app={url}"]
            for name in MAC_BROWSERS
            if _mac_installed(name)
        ]
    return [
        [executable, f"--app={url}"]
        for executable in map(shutil.which, LINUX_BROWSERS)
        if executable is not None
    ]


def open_window(url: str) -> None:
    if any(_spawn(argv) for argv in _app_commands(url)):
        return
    _open_fallback(url)


def _open_fallback(url: str) -> None:
    """Firefox has no app mode; a separate window is the closest thing to one."""
    firefox = shutil.which("firefox")
    if firefox is None or not _spawn([firefox, "--new-window", url]):
        webbrowser.open(url)
