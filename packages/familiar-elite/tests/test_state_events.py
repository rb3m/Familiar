"""State reconstruction and event producers operate on local journal dictionaries."""

import pytest
from familiar_elite.events import EventDetector
from familiar_elite.state import EliteState


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (
            {"event": "LoadGame", "Commander": "Mira", "Ship": "Asp", "FuelCapacity": 32},
            {"commander": "Mira", "ship": "Asp", "fuel_capacity": 32},
        ),
        (
            {
                "event": "Loadout",
                "ShipID": 2,
                "ShipName": "Explorer",
                "FuelCapacity": {"Main": 32, "Reserve": 0.5},
            },
            {"ship_id": 2, "ship_name": "Explorer", "fuel_capacity": 32},
        ),
        (
            {
                "event": "FSDJump",
                "StarSystem": "Sol",
                "StarPos": [0, 0, 0],
                "SystemSecurity": "$HIGH;",
                "SystemEconomy_Localised": "Industrial",
                "Population": 10,
                "SystemAllegiance": "Federation",
                "SystemFaction": {"Name": "Pilots"},
            },
            {
                "system": "Sol",
                "star_pos": [0, 0, 0],
                "security": "$HIGH;",
                "economy": "Industrial",
                "population": 10,
                "allegiance": "Federation",
                "faction": {"Name": "Pilots"},
            },
        ),
        ({"event": "StartJump", "StarClass": "K"}, {"star_class": "K"}),
        ({"event": "SupercruiseEntry"}, {"supercruise": True}),
        ({"event": "SupercruiseExit", "Body": "Earth"}, {"supercruise": False, "body": "Earth"}),
        ({"event": "ApproachBody", "Body": "Mars"}, {"body": "Mars"}),
        ({"event": "Docked", "StationName": "Galileo"}, {"docked": True, "station": "Galileo"}),
        ({"event": "Undocked", "StationName": "Galileo"}, {"docked": False}),
        ({"event": "DockingRequested"}, {"docking": "DockingRequested"}),
        ({"event": "DockingGranted", "LandingPad": 4}, {"landing_pad": 4}),
        ({"event": "DockingDenied", "Reason": "NoSpace"}, {"docking_reason": "NoSpace"}),
        ({"event": "Interdicted"}, {"interdicted": True}),
        ({"event": "EscapeInterdiction"}, {"interdicted": False}),
        ({"event": "HullDamage", "Health": 0.6}, {"hull": 0.6}),
        ({"event": "ShieldState", "ShieldsUp": False}, {"shields_up": False}),
        ({"event": "FuelScoop", "Total": 20}, {"fuel_level": 20}),
        ({"event": "FSSDiscoveryScan", "BodyCount": 14}, {"bodies": 14}),
        ({"event": "Died"}, {"alive": False}),
        ({"event": "Shutdown"}, {"shutdown": True}),
    ],
)
def test_state(event, expected):
    state = EliteState()
    state.apply(event)
    assert {key: getattr(state, key) for key in expected} == expected
    previous = state.to_dict()
    state.apply({"event": "Future", "Surprise": 42})
    assert state.to_dict() == previous


def test_scan_state():
    state = EliteState()
    scan = dict(
        event="Scan",
        BodyName="Sol 3",
        PlanetClass="Earthlike body",
        WasDiscovered=False,
        WasMapped=False,
        Landable=False,
        TerraformState="",
    )
    state.apply(scan)
    assert state.scans["Sol 3"] == {k: v for k, v in scan.items() if k != "event"}


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        ({"event": "FSDJump", "StarSystem": "Sol"}, "system_arrival"),
        ({"event": "StartJump", "StarClass": "M"}, "scoopable_star"),
        ({"event": "Interdicted"}, "interdiction"),
        ({"event": "ShieldState", "ShieldsUp": False}, "shields_down"),
        ({"event": "ShieldState", "ShieldsUp": True}, "shields_up"),
        ({"event": "DockingGranted", "LandingPad": 4}, "docking_granted"),
        ({"event": "DockingDenied", "Reason": "NoSpace"}, "docking_denied"),
        ({"event": "Docked", "StationName": "Galileo"}, "docked"),
        ({"event": "Undocked", "StationName": "Galileo"}, "undocked"),
        (
            {
                "event": "Scan",
                "BodyName": "A 1",
                "PlanetClass": "Water world",
                "WasDiscovered": False,
            },
            "first_discovery",
        ),
        ({"event": "FSSDiscoveryScan", "BodyCount": 10}, "discovery_scan"),
        ({"event": "HullDamage", "Health": 0.2}, "hull_damage"),
    ],
)
def test_producers_and_cooldowns(data, kind):
    now = [100.0]
    detector = EventDetector(clock=lambda: now[0])
    state = EliteState()
    state.apply(data)
    assert detector.journal(data, state, historical=True) == []
    detector = EventDetector(clock=lambda: now[0])
    (event,) = detector.journal(data, state)
    assert event.kind == kind and event.data["summary"]
    assert event.timestamp.tzinfo
    assert detector.journal(data, state) == []
    if kind == "docking_denied":
        assert event.data["reason"] == "NoSpace"
    now[0] += 100
    if kind != "hull_damage":
        assert detector.journal(data, state)


def test_crossings_and_thresholds():
    now = [0.0]
    detector = EventDetector(clock=lambda: now[0])
    flags = {"low_fuel": False, "over_heating": False, "shields_up": True}
    assert detector.status(flags) == []
    flags.update(low_fuel=True, over_heating=True, shields_up=False)
    assert {e.kind for e in detector.status(flags)} == {"low_fuel", "overheating", "shields_down"}
    now[0] = 100
    assert detector.status(flags) == []
    flags["low_fuel"] = False
    detector.status(flags)
    flags["low_fuel"] = True
    assert [e.kind for e in detector.status(flags)] == ["low_fuel"]
    assert detector.journal({"event": "StartJump", "StarClass": "L"}, EliteState()) == []
    assert detector.journal({"event": "Scan", "WasDiscovered": True}, EliteState()) == []
    assert (
        detector.journal(
            {"event": "Scan", "WasDiscovered": False, "PlanetClass": "Rocky body"}, EliteState()
        )
        == []
    )
    assert detector.journal({"event": "Unknown"}, EliteState()) == []
    assert detector._hull(0.9, False) == []
    assert detector._hull(0.7, False)[0].kind == "hull_damage"
    now[0] += 20
    assert detector._hull(0.65, False) == []
    assert detector._hull(0.49, False)[0].kind == "hull_damage"


def test_new_system_and_commander_clear_old_facts():
    state = EliteState()
    state.apply(
        {
            "event": "Location",
            "StarSystem": "Sol",
            "SystemSecurity_Localised": "High",
            "Population": 1000,
            "SystemFaction": {"Name": "Old"},
        }
    )
    state.apply({"event": "Scan", "BodyName": "Sol 1"})
    state.apply({"event": "FSDJump", "StarSystem": "Empty"})
    assert state.security is None and state.population is None
    assert state.faction is None and state.scans == {}
    state.apply({"event": "LoadGame", "Commander": "New"})
    assert state.system is None and state.commander == "New"
