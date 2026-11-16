from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from familiar_scs.events import EventDetector
from familiar_scs.knowledge import KnowledgePack
from familiar_scs.places import Place, PlaceIndex
from familiar_scs.telemetry import Snapshot
from familiar_scs.units import Units


@pytest.fixture
def detector():
    return EventDetector(lambda x, z: (x, z), PlaceIndex(places=()), KnowledgePack(), Units())


def kinds(events):
    for event in events:
        assert isinstance(event.data["summary"], str) and event.data["summary"]
        assert event.timestamp.tzinfo is not None
    return [event.kind for event in events]


@pytest.mark.parametrize(
    "flag,kind,priority",
    [
        ("fuel_warning", "fuel_low", "critical"),
        ("air_pressure_warning", "air_pressure", "critical"),
        ("air_pressure_emergency", "air_pressure", "critical"),
        ("oil_pressure_warning", "engine_warning", "critical"),
        ("water_temperature_warning", "engine_warning", "critical"),
        ("battery_voltage_warning", "engine_warning", "critical"),
        ("fined", "fined", "critical"),
        ("job_cancelled", "job_cancelled", "critical"),
        ("on_job", "job_started", "notable"),
        ("job_delivered", "job_delivered", "notable"),
        ("ferry", "ferry", "notable"),
        ("train", "train", "notable"),
        ("refuel_paid", "refuel_paid", "notable"),
    ],
)
def test_rising_edges(detector, flag, kind, priority):
    baseline = Snapshot(connected=True, engine_on=True)
    detector.feed(baseline)
    active = replace(baseline, **{flag: True})
    events = detector.feed(active)
    assert kinds(events) == [kind]
    assert events[0].priority == priority
    assert detector.feed(active) == []
    assert detector.feed(baseline) == []
    assert kinds(detector.feed(active)) == [kind]


def test_no_warnings_from_the_starting_state_or_a_parked_engine(detector):
    # Live drive: oil and battery lights were announced as the game connected, engine off.
    parked = Snapshot(connected=True, oil_pressure_warning=True, battery_voltage_warning=True)
    assert "engine_warning" not in kinds(detector.feed(parked))
    detector.feed(Snapshot(connected=True))
    assert "engine_warning" not in kinds(detector.feed(replace(parked, engine_on=False)))
    running = Snapshot(connected=True, engine_on=True)
    detector.feed(running)
    assert kinds(detector.feed(replace(running, oil_pressure_warning=True))) == ["engine_warning"]


def test_critical_details(detector):
    s = Snapshot(
        connected=True,
        fined=True,
        job_cancelled=True,
        fined_details={"offence": "speeding", "amount": 350},
        cancelled_details={"penalty": 1000},
    )
    events = {event.kind: event for event in detector.feed(s)}
    assert events["fined"].data["amount"] == 350
    assert "$350" in events["fined"].data["summary"]
    assert events["job_cancelled"].data["penalty"] == 1000


def test_job_and_transport_details(detector):
    s = Snapshot(
        connected=True,
        on_job=True,
        job_delivered=True,
        ferry=True,
        train=True,
        source_city="Boise",
        destination_city="Reno",
        source_company="Potato Co",
        destination_company="Market",
        cargo="potatoes",
        planned_distance_km=160.9344,
        job_remaining=timedelta(hours=9),
        delivered_details={
            "revenue": 2000,
            "earnedXp": 500,
            "cargoDamage": 0.12,
            "distanceKm": 160.9344,
        },
        ferry_details={"sourceName": "A", "targetName": "B", "payAmount": 10},
        train_details={"sourceName": "C", "targetName": "D", "payAmount": 20},
    )
    events = {e.kind: e for e in detector.feed(s)}
    started = events["job_started"].data
    assert started["cargo"] == "potatoes"
    assert started["deadline_remaining_s"] == 32400
    assert "100 mi" in started["summary"]
    delivered = events["job_delivered"].data
    assert delivered["damage_percent"] == 12
    assert delivered["xp"] == 500
    assert "12%" in delivered["summary"]
    assert events["ferry"].data["price"] == 10
    assert events["train"].data["source_name"] == "C"


def test_game_state(detector):
    assert detector.feed(Snapshot()) == []
    connected = Snapshot(connected=True)
    events = detector.feed(connected)
    assert [(e.kind, e.data["state"], e.data["ttl_s"]) for e in events] == [
        ("game_state", "connected", 0)
    ]
    assert detector.feed(connected) == []
    assert detector.feed(replace(connected, paused=True))[0].data["state"] == "paused"
    assert detector.feed(connected)[0].data["state"] == "unpaused"
    assert detector.feed(Snapshot())[0].data["state"] == "disconnected"


def test_state_hysteresis():
    places = PlaceIndex(places=(Place("A", "WY", 40, -105, 1), Place("B", "CO", 39, -105, 1)))
    detector = EventDetector(lambda x, z: (x, z), places, KnowledgePack(), Units())
    a = Snapshot(connected=True, x=40, z=-105)
    b = replace(a, x=39)
    for _ in range(3):
        assert "entered_state" not in kinds(detector.feed(a))
    for sample in (b, b, a, b, b):
        assert "entered_state" not in kinds(detector.feed(sample))
    event = detector.feed(b)[0]
    assert event.kind == "entered_state"
    assert event.data["from_state"] == "Wyoming"
    assert event.data["to_state"] == "Colorado"
    assert detector.feed(b) == []


def test_landmark_hysteresis():
    pack = KnowledgePack.load([Path(__file__).parent / "fixtures/scs_landmarks.yaml"])
    detector = EventDetector(lambda x, z: (x, z), PlaceIndex(places=()), pack, Units())
    near = Snapshot(connected=True, x=43.7339, z=-106.3402)
    events = detector.feed(near)
    assert "landmark_nearby" in kinds(events)
    event = next(e for e in events if e.kind == "landmark_nearby")
    assert event.data["facts"] == ["Kaycee is in Johnson County."]
    assert event.data["source"] == "https://example.org/kaycee"
    assert event.data["ttl_s"] == 120
    assert detector.feed(near) == []
    # About 6 km away: outside 5 km radius, within 7.5 km rearm boundary.
    detector.feed(replace(near, x=43.788))
    assert detector.feed(near) == []
    detector.feed(replace(near, x=45))
    assert kinds(detector.feed(near)) == ["landmark_nearby"]


def test_passing_town_session():
    places = PlaceIndex(places=(Place("A", "WY", 40, -105, 20),))
    detector = EventDetector(lambda x, z: (x, z), places, KnowledgePack(), Units())
    near = Snapshot(connected=True, x=40, z=-105)
    assert "passing_town" in kinds(detector.feed(near))
    detector.feed(replace(near, x=45))
    assert detector.feed(near) == []
    small = PlaceIndex(places=(Place("Small", "WY", 40, -105, 19),))
    detector = EventDetector(lambda x, z: (x, z), small, KnowledgePack(), Units())
    assert "passing_town" not in kinds(detector.feed(near))


def test_rest_cycle(detector):
    s = Snapshot(connected=True, rest_remaining=timedelta(minutes=60))
    assert "rest_soon" not in kinds(detector.feed(s))
    soon = replace(s, rest_remaining=timedelta(minutes=59))
    assert kinds(detector.feed(soon)) == ["rest_soon"]
    assert detector.feed(soon) == []
    detector.feed(replace(s, rest_remaining=None))
    assert detector.feed(soon) == []
    detector.feed(replace(s, rest_remaining=timedelta(hours=10)))
    assert kinds(detector.feed(soon)) == ["rest_soon"]


def test_deadline_once_per_job(detector):
    s = Snapshot(
        connected=True,
        on_job=True,
        job_remaining=timedelta(minutes=110),
        nav_estimated=timedelta(minutes=100),
    )
    assert "deadline_tight" not in kinds(detector.feed(s))
    tight = replace(s, job_remaining=timedelta(minutes=109))
    assert kinds(detector.feed(tight)) == ["deadline_tight"]
    detector.feed(s)
    assert detector.feed(tight) == []
    detector.feed(replace(s, on_job=False))
    assert "deadline_tight" in kinds(detector.feed(tight))


def test_speeding_duration_and_cooldown(detector):
    now = [0.0]
    detector.clock = lambda: now[0]
    s = Snapshot(connected=True, speed=97, speed_limit=80)
    assert "speeding" not in kinds(detector.feed(s))
    now[0] = 19.9
    assert detector.feed(s) == []
    now[0] = 20
    assert kinds(detector.feed(s)) == ["speeding"]
    now[0] = 619
    assert detector.feed(s) == []
    now[0] = 620
    assert kinds(detector.feed(s)) == ["speeding"]
    now[0] = 1000
    detector.feed(replace(s, paused=True))
    now[0] = 1220
    assert "speeding" not in kinds(detector.feed(s))
    now[0] = 1240
    assert kinds(detector.feed(s)) == ["speeding"]


@pytest.mark.parametrize(
    "changes", [{"speed_limit": 0}, {"speed": 96}, {"paused": True}, {"connected": False}]
)
def test_speeding_interrupted(detector, changes):
    now = [0.0]
    detector.clock = lambda: now[0]
    s = Snapshot(connected=True, speed=97, speed_limit=80)
    detector.feed(s)
    now[0] = 19
    detector.feed(replace(s, **changes))
    now[0] = 21
    assert "speeding" not in kinds(detector.feed(s))
    now[0] = 41
    assert "speeding" in kinds(detector.feed(s))
