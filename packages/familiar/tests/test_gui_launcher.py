"""Window opening picks an app-mode browser per platform, detached from our stdio."""

import subprocess

from familiar.gui import launcher


def capture(monkeypatch, platform, installed=(), which=None):
    calls = []
    monkeypatch.setattr(launcher.sys, "platform", platform)
    monkeypatch.setattr(launcher, "_mac_installed", lambda name: name in installed)
    monkeypatch.setattr(launcher.shutil, "which", which or (lambda name: None))
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: calls.append(["webbrowser", url]))

    def popen(argv, **kwargs):
        assert kwargs["stdout"] is subprocess.DEVNULL and kwargs["stderr"] is subprocess.DEVNULL
        calls.append(argv)

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    return calls


def test_mac_opens_installed_chrome_through_launch_services(monkeypatch):
    calls = capture(monkeypatch, "darwin", installed={"Google Chrome", "Microsoft Edge"})
    launcher.open_window("http://127.0.0.1:8765/?token=t")
    assert calls == [
        ["open", "-na", "Google Chrome", "--args", "--app=http://127.0.0.1:8765/?token=t"]
    ]


def test_mac_without_app_mode_browser_uses_default_browser(monkeypatch):
    calls = capture(monkeypatch, "darwin")
    launcher.open_window("http://x")
    assert calls == [["webbrowser", "http://x"]]


def test_linux_uses_first_browser_on_path(monkeypatch):
    found = {"google-chrome": "/usr/bin/google-chrome"}
    calls = capture(monkeypatch, "linux", which=found.get)
    launcher.open_window("http://x")
    assert calls == [["/usr/bin/google-chrome", "--app=http://x"]]


def test_failed_spawn_falls_through_to_firefox(monkeypatch):
    found = {"chromium": "/usr/bin/chromium", "firefox": "/usr/bin/firefox"}
    calls = capture(monkeypatch, "linux", which=found.get)

    def popen(argv, **kwargs):
        if "chromium" in argv[0]:
            raise OSError("gone")
        calls.append(argv)

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    launcher.open_window("http://x")
    assert calls == [["/usr/bin/firefox", "--new-window", "http://x"]]
