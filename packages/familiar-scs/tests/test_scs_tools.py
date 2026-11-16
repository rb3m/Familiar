import asyncio
import json
from pathlib import Path

import pytest
from familiar.interfaces import ConfigError
from familiar_scs import telemetry, tools


def test_record_flush_and_cleanup(tmp_path, monkeypatch):
    payload = {"game": {"connected": True}, "truck": {"speed": 50}}
    path = tmp_path / "record.jsonl"
    closed = []

    class Source:
        def __init__(self, url, interval_s):
            assert url == "http://localhost/test"
            assert interval_s == 2

        async def payloads(self):
            yield payload
            # This is observable before the recorder finishes, so it checks flush.
            item = json.loads(path.read_text())
            assert item["telemetry"] == payload
            assert isinstance(item["ts"], float)
            raise asyncio.CancelledError

        async def aclose(self):
            closed.append(True)

    monkeypatch.setattr(telemetry, "HttpTelemetry", Source)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(telemetry.record("http://localhost/test", path, 2))
    assert closed == [True]


def test_record_settings(monkeypatch):
    calls = []

    async def record(url, path, interval_s):
        calls.append((url, path, interval_s))

    monkeypatch.setattr(tools, "record_telemetry", record)
    asyncio.run(
        tools.record({"url": "http://localhost/test", "poll_interval_s": 2}, Path("log.jsonl"))
    )
    assert calls == [("http://localhost/test", Path("log.jsonl"), 2)]


def test_watch_finite_replay(tmp_path):
    path = tmp_path / "replay.jsonl"
    path.write_text(
        json.dumps(
            {"ts": 1, "telemetry": {"game": {"connected": True}, "truck": {"fuelWarningOn": True}}}
        )
        + "\n"
    )
    lines = []
    asyncio.run(tools.watch({"replay": str(path)}, "ets2", lines.append))
    assert any("ambient game_state: Game connected." in line for line in lines)
    assert any("critical fuel_low:" in line for line in lines)
    assert any("Location:" in line for line in lines)


def test_watch_unknown_game():
    with pytest.raises(ConfigError, match="Unknown SCS game"):
        asyncio.run(tools.watch({}, "other"))
