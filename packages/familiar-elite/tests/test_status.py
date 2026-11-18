"""Documented flag positions, raw half-pips and retained valid snapshots."""

import pytest
from familiar_elite.status import StatusReader


@pytest.mark.parametrize(
    ("field", "bit", "name"),
    [
        ("Flags", 0, "docked"),
        ("Flags", 3, "shields_up"),
        ("Flags", 19, "low_fuel"),
        ("Flags", 20, "over_heating"),
        ("Flags", 24, "in_main_ship"),
        ("Flags", 26, "in_srv"),
        ("Flags", 28, "night_vision"),
        ("Flags2", 0, "on_foot"),
        ("Flags2", 5, "aim_down_sight"),
        ("Flags2", 12, "glide_mode"),
    ],
)
def test_known_bits(tmp_path, write_snapshot, field, bit, name):
    write_snapshot(tmp_path, **{"Flags": 0, field: 1 << bit})
    reader = StatusReader(tmp_path)
    reader.poll()
    assert reader.flags[name]
    assert sum(reader.flags.values()) == 1


def test_torn_retains_age_and_values(tmp_path, write_snapshot):
    now = [100.0]
    reader = StatusReader(tmp_path, clock=lambda: now[0])
    write_snapshot(
        tmp_path,
        Flags=1 << 24,
        Pips=[4, 4, 4],
        Fuel={"FuelMain": 16, "FuelReservoir": 0.5},
        GuiFocus=0,
        LegalState="Clean",
        Destination={"Name": "Sol"},
        Cargo=3,
    )
    reader.poll()
    assert reader.fresh()
    now[0] = 111.0
    reader.poll()
    assert reader.acquired_at == 111 and reader.fresh()
    now[0] = 130.0
    for invalid in ("", '{"Flags":', "null", "{}", "[]"):
        (tmp_path / "Status.json").write_text(invalid)
        reader.poll()
        assert reader.acquired_at == 111 and not reader.fresh()
        assert reader.fuel_main == 16
    assert reader.fuel_reservoir == 0.5
    assert reader.pips == [4, 4, 4]
    assert reader.gui_focus == 0 and reader.legal_state == "Clean"
    assert reader.destination == {"Name": "Sol"} and reader.cargo == 3
    write_snapshot(tmp_path, Flags=0)
    reader.poll()
    assert reader.acquired_at == 130 and reader.fresh()


def test_route_and_cargo(tmp_path, write_snapshot):
    reader = StatusReader(tmp_path)
    reader.poll()
    assert reader.cargo is None and reader.jumps_remaining("Sol") is None
    write_snapshot(tmp_path, "Cargo", Count=7, Inventory=[])
    write_snapshot(tmp_path, "NavRoute", Route=[{"StarSystem": "Sol"}, {"StarSystem": "A"}])
    reader.poll()
    assert reader.cargo == 7
    assert reader.jumps_remaining("Sol") == 1
    assert reader.jumps_remaining("A") == 0
    assert reader.jumps_remaining("Unknown") is None
    (tmp_path / "NavRoute.json").write_text("{")
    reader.poll()
    assert reader.jumps_remaining("Sol") == 1


@pytest.mark.parametrize("bad", [{"Flags2": None}, {"Flags2": "x"}, {"Fuel": []}])
def test_malformed_fields_keep_last_snapshot(tmp_path, write_snapshot, bad):
    reader = StatusReader(tmp_path)
    write_snapshot(tmp_path, Flags=1 << 24)
    reader.poll()
    previous = reader.acquired_at
    write_snapshot(tmp_path, Flags=0, **bad)
    reader.poll()
    assert reader.acquired_at == previous
    assert reader.flags["in_main_ship"]


def test_unchanged_status_stays_fresh(tmp_path):
    from familiar_elite.status import StatusReader

    now = [0.0]
    (tmp_path / "Status.json").write_text('{"Flags": 0}')
    reader = StatusReader(tmp_path, clock=lambda: now[0])
    reader.poll()
    now[0] = 60.0
    reader.poll()
    assert reader.fresh()


def test_resurrect_restores_alive():
    from familiar_elite.state import EliteState

    state = EliteState()
    state.apply({"event": "Died"})
    state.apply({"event": "Resurrect", "Option": "rebuy"})
    assert state.alive
