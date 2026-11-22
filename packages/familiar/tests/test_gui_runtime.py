"""Single-instance runtime file: private, detects live servers, cleans up stale ones."""

import stat

import httpx
from familiar.gui import runtime
from familiar.gui.timeouts import RUNNING_CHECK_TIMEOUT_S


def test_write_is_private_and_stale_file_is_removed(tmp_path, monkeypatch):
    path = tmp_path / "gui.json"
    runtime.write("http://127.0.0.1:1/?token=t", "t", path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def refuse(*args, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(runtime.httpx, "get", refuse)
    assert runtime.running_url(path) is None
    assert not path.exists()


def test_live_server_is_reused(tmp_path, monkeypatch):
    path = tmp_path / "gui.json"
    runtime.write("http://127.0.0.1:8765/?token=abc", "abc", path)
    seen = {}

    def ok(url, headers, timeout):
        seen.update(url=url, token=headers["X-Familiar-Token"], timeout=timeout)
        return httpx.Response(200, json={})

    monkeypatch.setattr(runtime.httpx, "get", ok)
    assert runtime.running_url(path) == "http://127.0.0.1:8765/?token=abc"
    assert seen == {
        "url": "http://127.0.0.1:8765/api/state",
        "token": "abc",
        "timeout": RUNNING_CHECK_TIMEOUT_S,
    }
