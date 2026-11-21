"""Atomic envelopes, locks, scheduling, and failure isolation."""

import asyncio
import json
import stat
from datetime import UTC, datetime

import pytest
from familiar_desk.feed import Feed, WriterBusyError
from familiar_desk.paths import feed_dir, feed_path, lock_path

NOW = datetime(2026, 11, 20, 18, tzinfo=UTC)


class StubSource:
    def __init__(self, name="stub", interval=60, fail=False):
        self.name = name
        self.interval_s = interval
        self.fail = fail
        self.calls = 0

    async def sample(self, now):
        self.calls += 1
        if self.fail:
            raise ValueError("offline failure")
        return {"value": 42}, [{"id": str(i)} for i in range(70)]


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    return tmp_path


@pytest.mark.asyncio
async def test_envelope_permissions_and_isolation(runtime):
    good, bad = StubSource(), StubSource("broken", fail=True)
    feed = Feed([good, bad], clock=lambda: NOW)
    await feed.run(asyncio.Event(), once=True)
    data = json.loads(feed_path().read_text())
    assert set(data) == {
        "schema_version",
        "instance_id",
        "sequence",
        "generated_at",
        "sources",
        "events",
    }
    assert data["sequence"] == 2 and data["schema_version"] == 1
    assert data["generated_at"] == NOW.isoformat()
    assert len(data["events"]) == 50
    entry = data["sources"]["stub"]
    assert set(entry) == {"observed_at", "expires_at", "status", "error", "data"}
    assert entry["status"] == "ok" and entry["error"] is None
    assert data["sources"]["broken"]["error"] == "offline failure"
    assert stat.S_IMODE(feed_path().stat().st_mode) == 0o600
    assert stat.S_IMODE(feed_dir().stat().st_mode) == 0o700
    assert list(feed_dir().glob(".desk-feed-*")) == []


def test_single_writer(runtime):
    first, second = Feed([]), Feed([])
    first.acquire()
    try:
        with pytest.raises(WriterBusyError, match="already running"):
            second.acquire()
    finally:
        first.close()
    second.acquire()
    second.close()
    assert lock_path().exists()


@pytest.mark.asyncio
async def test_independent_sources_and_stop(runtime):
    fast, slow = StubSource(interval=0.001), StubSource("slow", interval=100)
    stop = asyncio.Event()
    feed = Feed([fast, slow], clock=lambda: NOW)
    original = fast.sample

    async def sample(now):
        result = await original(now)
        if fast.calls == 3:
            stop.set()
        return result

    fast.sample = sample
    await asyncio.wait_for(feed.run(stop), 2)
    assert fast.calls == 3 and slow.calls == 1


@pytest.mark.asyncio
async def test_failed_replace_preserves_snapshot(runtime, monkeypatch):
    feed = Feed([], clock=lambda: NOW)
    feed.write()
    old = feed_path().read_bytes()

    def fail(*args):
        raise OSError("rename failed")

    monkeypatch.setattr("familiar_desk.feed.os.replace", fail)
    with pytest.raises(OSError):
        feed.write()
    assert feed_path().read_bytes() == old
    assert list(feed_dir().glob(".desk-feed-*")) == []
