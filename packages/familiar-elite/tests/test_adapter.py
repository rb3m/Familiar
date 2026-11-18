"""Plugin contracts, silent startup and safe watch validity."""

import asyncio

import pytest
from familiar.interfaces import ConfigError, GameAdapter, Watchable
from familiar_elite.adapter import EliteAdapter


def test_bootstrap_and_watch(journal_dir, write_snapshot):
    adapter = EliteAdapter({"journal_dir": str(journal_dir)})
    assert isinstance(adapter, GameAdapter) and isinstance(adapter, Watchable)
    assert adapter._poll() == []
    assert adapter.state.commander == "Test Commander"
    assert not adapter.watch_sample().valid
    assert adapter.describe() == ""
    write_snapshot(journal_dir, Flags=(1 << 24) | (1 << 3), Fuel={"FuelMain": 16}, Cargo=4)
    write_snapshot(journal_dir, "NavRoute", Route=[{"StarSystem": "Sol"}, {"StarSystem": "A"}])
    assert adapter._poll() == []
    sample = adapter.watch_sample()
    assert sample.valid and not sample.predictable
    assert sample.values == {"fuel": 50, "cargo": 4, "jumps_remaining": 1}
    assert sample.context == "Test Commander:4"
    assert adapter.watch_sample().timestamp == sample.timestamp
    assert {k: v.unit for k, v in adapter.watchables().items()} == {
        "fuel": "%",
        "cargo": "count",
        "jumps_remaining": "count",
    }
    assert "Sol" in adapter.describe() and "50" in adapter.describe()
    assert adapter.tools() == [] and "actions" not in adapter.capabilities
    with adapter.journal.path.open("a") as stream:
        stream.write('{"event":"DockingDenied","Reason":"NoSpace"}\n')
    assert adapter._poll()[0].data["reason"] == "NoSpace"


@pytest.mark.parametrize(("flags", "flags2"), [(1 << 26, 0), (1 << 24, 1), (0, 0)])
def test_invalid_modes(journal_dir, write_snapshot, flags, flags2):
    adapter = EliteAdapter({"journal_dir": str(journal_dir)})
    write_snapshot(journal_dir, Flags=flags, Flags2=flags2)
    adapter._poll()
    assert not adapter.watch_sample().valid


async def test_snapshot_stale_shutdown_and_close(journal_dir, write_snapshot):
    adapter = EliteAdapter({"journal_dir": str(journal_dir)})
    write_snapshot(journal_dir, Flags=1 << 24)
    adapter._poll()
    snapshot = await adapter.snapshot()
    assert snapshot["connected"] and snapshot["driving"] and not snapshot["paused"]
    assert adapter.watch_sample().values["fuel"] is None
    adapter.status.clock = lambda: adapter.status.acquired_at + 11
    assert not adapter.watch_sample().valid
    assert not (await adapter.snapshot())["connected"]
    adapter.status.clock = lambda: adapter.status.acquired_at
    adapter.state.apply({"event": "Shutdown"})
    assert not adapter.watch_sample().valid
    assert adapter.describe() == ""
    await adapter.aclose()
    assert adapter.closed and adapter.journal.closed


async def test_async_events(journal_dir):
    adapter = EliteAdapter({"journal_dir": str(journal_dir), "poll_interval_s": 0.001})
    adapter._poll()
    with adapter.journal.path.open("a") as stream:
        stream.write('{"event":"Docked","StationName":"Galileo"}\n')
    iterator = adapter.events()
    assert (await asyncio.wait_for(anext(iterator), 1)).kind == "docked"
    await iterator.aclose()
    assert adapter.closed


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan")])
def test_invalid_interval(interval):
    with pytest.raises(ConfigError):
        EliteAdapter({"poll_interval_s": interval})


def test_rescan_clears_old_status(journal_dir, write_snapshot, tmp_path):
    adapter = EliteAdapter({"journal_dir": str(journal_dir)})
    write_snapshot(journal_dir, Flags=1 << 24)
    adapter._poll()
    assert adapter.watch_sample().valid
    new_directory = tmp_path / "new"
    new_directory.mkdir()
    adapter.settings["journal_dir"] = str(new_directory)
    adapter.rescan()
    assert not adapter.watch_sample().valid
    assert adapter.state.system is None
    assert adapter.journal.directory == new_directory
