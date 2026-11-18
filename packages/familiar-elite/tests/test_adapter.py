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


def test_describe_full_awareness(journal_dir, write_snapshot):
    adapter = EliteAdapter({"journal_dir": str(journal_dir)})
    adapter._poll()
    adapter.state.apply(
        {
            "event": "Loadout",
            "Ship": "Asp",
            "ShipName": "Wanderer",
            "ShipIdent": "WA-01",
            "HullHealth": 0.85,
            "FuelCapacity": {"Main": 32},
            "CargoCapacity": 16,
            "MaxJumpRange": 43.25,
            "Modules": [{"Item": "int_cargorack_size2_class1"}],
        }
    )
    adapter.state.apply(
        {
            "event": "Location",
            "StarSystem": "Sol",
            "SystemAllegiance": "Federation",
            "SystemEconomy_Localised": "Industrial",
            "SystemSecurity_Localised": "High",
            "SystemGovernment_Localised": "Democracy",
            "Population": 1000,
            "SystemFaction": {"Name": "Pilots"},
        }
    )
    adapter.state.apply({"event": "StartJump", "StarClass": "G"})
    adapter.state.apply({"event": "FSSDiscoveryScan", "BodyCount": 12})
    adapter.state.apply(
        {
            "event": "Scan",
            "BodyName": "Sol 3",
            "PlanetClass": "Earthlike body",
            "WasDiscovered": False,
            "Landable": False,
        }
    )
    adapter.state.apply(
        {
            "event": "Scan",
            "BodyName": "Sol 4",
            "PlanetClass": "Water world",
            "Landable": True,
        }
    )
    adapter.state.apply(
        {
            "event": "Docked",
            "StationName": "Galileo",
            "StationType": "Outpost",
            "StationEconomy_Localised": "Industrial",
            "StationServices": ["market", "searchrescue"],
            "timestamp": "3312-11-18T12:00:00Z",
        }
    )
    write_snapshot(
        journal_dir,
        Flags=(1 << 24) | (1 << 3) | (1 << 20),
        Fuel={"FuelMain": 16},
        Balance=123456,
        LegalState="Clean",
        Pips=[8, 2, 2],
        Heat=110,
        Cargo=4,
    )
    write_snapshot(
        journal_dir,
        "Cargo",
        Count=4,
        Inventory=[
            {"Name": "gold", "Name_Localised": "Gold", "Count": 3, "Stolen": 1},
            {"Name": "occupiedescapepod", "Count": 1, "MissionID": 42},
        ],
    )
    adapter._poll_status()
    text = adapter.describe()
    for fact in (
        "Asp; Wanderer; WA-01",
        "hull 85 percent",
        "Shields: not fitted",
        "16 / 32 tonnes (50 percent)",
        "4 tonnes used; capacity 16 tonnes",
        "Gold: 3 tonnes (stolen 1 tonnes)",
        "occupiedescapepod: 1 tonnes (mission cargo)",
        "43.25 light-years",
        "123456 credits",
        "legal state Clean",
        "pips SYS/ENG/WEP 4/1/1",
        "heat 110 percent",
        "star class: G",
        "allegiance: Federation",
        "security: High",
        "government: Democracy",
        "population: 1000",
        "controlling faction: Pilots",
        "FSS body count: 12",
        "Our scans: 2 bodies",
        "1 first discoveries",
        "Sol 3 (Earthlike body, first discovery)",
        "Sol 4 (Water world, landable)",
        "Docked: Galileo; type: Outpost; economy: Industrial; services: market, searchrescue",
        "Game time (UTC): 3312-11-18T12:00:00Z",
    ):
        assert fact in text


@pytest.mark.parametrize("shield_flag,expected", [(0, "down"), (1 << 3, "up")])
def test_describe_partial_shields_and_missing_cargo(
    journal_dir, write_snapshot, shield_flag, expected
):
    adapter = EliteAdapter({"journal_dir": str(journal_dir)})
    adapter.state.apply(
        {
            "event": "Loadout",
            "Modules": [{"Item": "Int_ShieldGenerator_Size3_Class5"}],
        }
    )
    write_snapshot(journal_dir, Flags=(1 << 24) | shield_flag)
    adapter._poll_status()
    text = adapter.describe()
    assert f"Shields: {expected}" in text
    assert "unknown" not in text
    assert "Cargo:" not in text and "balance" not in text and "heat" not in text
    assert "Game time (UTC): 2026-11-17T12:01:00Z" in text


def test_cargo_top_items_and_torn_snapshot(journal_dir, write_snapshot):
    adapter = EliteAdapter({"journal_dir": str(journal_dir)})
    write_snapshot(journal_dir, Flags=1 << 24)
    write_snapshot(
        journal_dir,
        "Cargo",
        Count=28,
        Inventory=[{"Name": f"item-{n}", "Count": n} for n in range(1, 8)],
    )
    adapter._poll_status()
    text = adapter.describe()
    assert "28 tonnes used" in text and "2 more cargo types" in text
    assert "item-7: 7 tonnes" in text and "item-1:" not in text
    (journal_dir / "Cargo.json").write_text('{"Count":')
    adapter._poll_status()
    assert adapter.describe() == text
