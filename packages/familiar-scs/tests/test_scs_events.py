from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from familiar_scs.events import EventDetector
from familiar_scs.knowledge import KnowledgePack
from familiar_scs.places import COUNTRY_NAMES, Place, PlaceIndex
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
    assert "100 miles" in started["summary"]
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


@pytest.mark.parametrize(
    "population,distance,expected",
    [(19999, 0, False), (20000, 0, True), (20000, 3, True), (20000, 3.01, False)],
)
def test_passing_european_town(population, distance, expected):
    detector = EventDetector(
        lambda x, z: (x, z),
        PlaceIndex(places=()),
        KnowledgePack(),
        Units("metric", "ets2"),
        region_names=COUNTRY_NAMES,
    )
    place = Place("Town", "DE", 50, 10, distance_km=distance, population=population)
    events = detector._town(place)
    assert bool(events) is expected
    if expected:
        assert events[0].kind == "passing_town"
        assert events[0].data["summary"] == "Passing Town, Germany."
        assert events[0].data["state"] == "Germany"
        assert detector._town(place) == []


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


@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("system,travel", [("metric", "412 kilometers"), ("imperial", "256 miles")])
@pytest.mark.parametrize("deadline_hours,on_time", [(14, True), (5, False)])
def test_rested_with_route(detector, paused, system, travel, deadline_hours, on_time):
    detector.units = Units(system)
    before = Snapshot(
        connected=True,
        paused=paused,
        on_job=True,
        game_time="0001-01-05T22:00:00Z",
        rest_remaining=timedelta(minutes=30),
        destination_city="Bayonne",
        nav_distance_km=412,
        nav_estimated=timedelta(hours=5, minutes=10),
        job_remaining=timedelta(hours=24),
    )
    assert "rest_soon" in kinds(detector.feed(before))
    after = replace(
        before,
        game_time="0001-01-06T07:00:00Z",
        rest_remaining=timedelta(hours=11),
        job_remaining=timedelta(hours=deadline_hours),
    )
    event = next(e for e in detector.feed(after) if e.kind == "rested")
    assert event.priority == "notable"
    assert event.data == {
        "slept_s": 9 * 3600,
        "rest_remaining_s": 11 * 3600,
        "destination_city": "Bayonne",
        "nav_distance_km": 412,
        "estimated_s": 310 * 60,
        "deadline_remaining_s": deadline_hours * 3600,
        "on_time": on_time,
        "summary": (
            f"Rested 9 hours. {travel} to Bayonne, about 5 hours 10 minutes of driving; "
            f"deadline in {deadline_hours} hours ({'comfortable' if on_time else 'tight'}). "
            "Next rest needed in 11 hours (game time)."
        ),
    }
    assert "rested" not in kinds(detector.feed(after))
    assert "rest_soon" in kinds(detector.feed(before))


@pytest.mark.parametrize(
    "before,after,expected",
    [
        (1, 4, True),
        (1, 3.99, False),
        (4, 4, False),
        (4, 3, False),
        (None, 11, False),
        (1, None, False),
    ],
)
def test_rested_timer_threshold(detector, before, after, expected):
    s = Snapshot(connected=True, rest_remaining=timedelta(hours=before) if before else None)
    detector.feed(s)
    events = detector.feed(replace(s, rest_remaining=timedelta(hours=after) if after else None))
    assert ("rested" in kinds(events)) is expected


def test_rested_ignores_first_snapshot_and_reconnect(detector):
    s = Snapshot(connected=True, rest_remaining=timedelta(hours=11))
    assert "rested" not in kinds(detector.feed(s))
    detector.feed(replace(s, rest_remaining=timedelta(minutes=30)))
    detector.feed(Snapshot())
    assert "rested" not in kinds(detector.feed(s))


@pytest.mark.parametrize("on_job,route", [(False, 412), (True, 0)])
def test_rested_without_job_or_route(detector, on_job, route):
    s = Snapshot(
        connected=True,
        on_job=on_job,
        nav_distance_km=route,
        destination_city="Bayonne",
        nav_estimated=timedelta(hours=5),
        job_remaining=timedelta(hours=14),
        rest_remaining=timedelta(hours=1),
        game_time="0001-01-05T22:00:00",
    )
    detector.feed(s)
    event = next(
        e
        for e in detector.feed(
            replace(s, rest_remaining=timedelta(hours=11), game_time="0001-01-06T07:00:00")
        )
        if e.kind == "rested"
    )
    assert event.data == {
        "slept_s": 9 * 3600,
        "rest_remaining_s": 11 * 3600,
        "summary": "Rested 9 hours. Next rest needed in 11 hours (game time).",
    }


@pytest.mark.parametrize(
    "before,after",
    [
        ("", ""),
        ("invalid", "0001-01-06T07:00:00"),
        ("0001-01-06T07:00:00", "invalid"),
        ("0001-01-06T07:00:00", "0001-01-05T22:00:00"),
    ],
)
def test_rested_omits_unavailable_sleep_and_route_fields(detector, before, after):
    s = Snapshot(
        connected=True,
        on_job=True,
        nav_distance_km=160.9344,
        game_time=before,
        rest_remaining=timedelta(hours=1),
    )
    detector.feed(s)
    event = next(
        e
        for e in detector.feed(replace(s, game_time=after, rest_remaining=timedelta(hours=11)))
        if e.kind == "rested"
    )
    assert event.data == {
        "nav_distance_km": 160.9344,
        "rest_remaining_s": 11 * 3600,
        "summary": "Rested. 100 miles. Next rest needed in 11 hours (game time).",
    }


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


@pytest.mark.parametrize("game,currency", [("ats", "$"), ("ets2", "€")])
def test_toll_summary(detector, game, currency):
    detector.units = Units(game=game)
    baseline = Snapshot(connected=True)
    detector.feed(baseline)
    s = replace(baseline, tollgate=True, tollgate_details={"payAmount": 12})
    event = detector.feed(s)[0]
    assert event.kind == "tollgate" and event.priority == "ambient"
    assert event.data["summary"] == f"Paid a toll of {currency}12."
    assert event.data["price"] == 12
    assert detector.feed(s) == []


@pytest.fixture
def timed_detector(detector):
    now = [0.0]
    detector.clock = lambda: now[0]
    return detector, now


def jam_snapshot(**changes):
    return replace(
        Snapshot(connected=True, engine_on=True, speed=20, speed_limit=90, nav_distance_km=100),
        **changes,
    )


def jam_events(detector, now, snapshot, start, end):
    events = []
    for second in range(start, end + 1):
        now[0] = float(second)
        events.extend(e for e in detector.feed(snapshot) if e.kind == "traffic_jam")
    return events


def test_jam_window_city_and_cooldown(timed_detector):
    detector, now = timed_detector
    detector.units = Units("metric", "ets2")
    detector.region_names = COUNTRY_NAMES
    detector.places = PlaceIndex(places=(Place("Paris", "FR", 0, 0, population=2_000_000),))
    s = jam_snapshot(nav_estimated=timedelta(minutes=30))
    assert jam_events(detector, now, s, 0, 119) == []
    now[0] = 120
    event = next(
        e
        for e in detector.feed(replace(s, nav_estimated=timedelta(minutes=33)))
        if e.kind == "traffic_jam"
    )
    assert event.priority == "notable"
    assert event.data["average_speed"] == "20 kilometers per hour"
    assert event.data["speed_limit"] == "90 kilometers per hour"
    assert event.data["minutes_stuck"] == 2
    assert event.data["name"] == "Paris" and event.data["region"] == "France"
    assert event.data["population"] == 2_000_000
    assert event.data["nav_estimated_growth_s"] == 180
    assert jam_events(detector, now, s, 121, 200) == []
    # A recovered road rearms the detector, but does not bypass the cooldown.
    assert jam_events(detector, now, replace(s, speed=70), 201, 231) == []
    assert jam_events(detector, now, s, 232, 1019) == []
    assert len(jam_events(detector, now, s, 1020, 1020)) == 1
    # Even after cooldown, the same unbroken jam is announced only once.
    assert jam_events(detector, now, s, 1021, 2000) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"speed_limit": 50},
        {"park_brake_on": True},
        {"engine_on": False},
        {"paused": True},
        {"connected": False},
        {"nav_distance_km": 1},
    ],
)
def test_no_jam_when_ineligible(timed_detector, changes):
    detector, now = timed_detector
    assert jam_events(detector, now, jam_snapshot(**changes), 0, 240) == []


@pytest.mark.parametrize("population,latitude", [(99_999, 0), (2_000_000, 1)])
def test_jam_population_requires_nearby_big_city(timed_detector, population, latitude):
    detector, now = timed_detector
    detector.places = PlaceIndex(places=(Place("Town", "WY", latitude, 0, population=population),))
    event = jam_events(detector, now, jam_snapshot(), 0, 120)[0]
    assert event.data["name"] == "Town"
    assert "population" not in event.data
    assert "nav_estimated_growth_s" not in event.data
    assert "miles per hour" in event.data["average_speed"]


def test_jam_requires_most_of_window_slow(timed_detector):
    detector, now = timed_detector
    s = jam_snapshot()
    assert jam_events(detector, now, replace(s, speed=40), 0, 40) == []
    assert jam_events(detector, now, s, 41, 120) == []
    assert len(jam_events(detector, now, s, 121, 140)) == 1


def test_jam_stop_and_go_alternative(timed_detector):
    detector, now = timed_detector
    s = jam_snapshot(speed=35, speed_limit=120)
    assert jam_events(detector, now, s, 0, 120) == []  # average above 25, no stops
    now[0] = 121
    detector.feed(replace(s, speed=5))
    now[0] = 122
    detector.feed(s)
    now[0] = 123
    assert "traffic_jam" in kinds(detector.feed(replace(s, speed=5)))


@pytest.mark.parametrize(
    "start,end,delta,expected",
    [
        (60, 38.4, 1, True),
        (60, 38.5, 1, False),
        (39, 0, 1, False),
        (80, 40, 2, False),
        (80, 40, 0, False),
    ],
)
def test_hard_braking_threshold(timed_detector, start, end, delta, expected):
    detector, now = timed_detector
    s = Snapshot(connected=True, speed=start)
    detector.feed(s)
    now[0] = delta
    assert "hard_braking" not in kinds(detector.feed(replace(s, speed=end)))
    now[0] += 5.01
    events = detector.feed(replace(s, speed=end))
    assert ("hard_braking" in kinds(events)) is expected
    if expected:
        event = next(e for e in events if e.kind == "hard_braking")
        assert event.priority == "notable"
        assert event.data["deceleration_mps2"] >= 6


def damaged_snapshot(**changes):
    from familiar_scs.telemetry import TrailerDamage

    return Snapshot(connected=True, trailer_damage=TrailerDamage(**changes))


@pytest.mark.parametrize("field", ["cargo", "body", "chassis"])
def test_collision_damage_and_cooldown(timed_detector, field):
    detector, now = timed_detector
    s = damaged_snapshot(slots=(0,))
    assert "collision" not in kinds(detector.feed(s))
    now[0] = 1
    damaged = damaged_snapshot(slots=(0,), **{field: 0.005})
    event = detector.feed(damaged)[0]
    assert event.kind == "collision" and event.priority == "critical"
    assert event.data["damage_increase_percent"] == 0.5
    assert event.data["cargo_damaged"] is (field == "cargo")
    now[0] = 2
    assert detector.feed(damaged_snapshot(slots=(0,), **{field: 0.02})) == []
    now[0] = 31
    assert kinds(detector.feed(damaged_snapshot(slots=(0,), **{field: 0.03}))) == ["collision"]


@pytest.mark.parametrize(
    "changes",
    [
        {"slots": (1,), "body": 0.5},
        {"slots": (), "cargo": 0.5},
        {"slots": (0,), "wheels": 0.5},
        {"slots": (0,), "body": 0.0049},
    ],
)
def test_no_collision_for_attach_detach_wheels_or_small_damage(timed_detector, changes):
    detector, now = timed_detector
    detector.feed(damaged_snapshot(slots=(0,)))
    now[0] = 1
    assert "collision" not in kinds(detector.feed(damaged_snapshot(**changes)))


@pytest.mark.parametrize("delay", [0, 1, 5])
def test_braking_collision_deduplication(timed_detector, delay):
    detector, now = timed_detector
    s = replace(damaged_snapshot(slots=(0,)), speed=80)
    detector.feed(s)
    now[0] = 1
    slowed = replace(s, speed=40)
    assert "hard_braking" not in kinds(detector.feed(slowed))
    now[0] += delay
    collision = replace(slowed, trailer_damage=replace(s.trailer_damage, cargo=0.01))
    events = detector.feed(collision)
    assert kinds(events) == ["collision"]
    assert events[0].data["hard_braking"]
    now[0] += 6
    assert detector.feed(collision) == []


def test_braking_cooldown(timed_detector):
    detector, now = timed_detector
    s = Snapshot(connected=True, speed=80)
    detector.feed(s)
    now[0] = 1
    detector.feed(replace(s, speed=40))
    now[0] = 7
    assert kinds(detector.feed(replace(s, speed=40))) == ["hard_braking"]
    now[0] = 8
    detector.feed(s)
    now[0] = 9
    detector.feed(replace(s, speed=40))
    now[0] = 15
    assert detector.feed(replace(s, speed=40)) == []
    now[0] = 67
    detector.feed(s)
    now[0] = 68
    detector.feed(replace(s, speed=40))
    now[0] = 74
    assert kinds(detector.feed(replace(s, speed=40))) == ["hard_braking"]


@pytest.mark.parametrize("changes", [{"paused": True}, {"connected": False}])
def test_close_calls_ignore_inactive_snapshots(timed_detector, changes):
    detector, now = timed_detector
    s = replace(damaged_snapshot(slots=(0,)), speed=80)
    detector.feed(s)
    now[0] = 1
    inactive = replace(s, speed=0, trailer_damage=replace(s.trailer_damage, body=0.1), **changes)
    assert not {"hard_braking", "collision"}.intersection(kinds(detector.feed(inactive)))
    now[0] = 2
    resumed = replace(inactive, connected=True, paused=False)
    assert not {"hard_braking", "collision"}.intersection(kinds(detector.feed(resumed)))
    now[0] = 8
    assert detector.feed(resumed) == []


def test_collision_first_snapshot_and_simultaneous_braking(timed_detector):
    detector, now = timed_detector
    s = replace(damaged_snapshot(slots=(0,), body=0.1), speed=80)
    assert "collision" not in kinds(detector.feed(s))
    now[0] = 1
    collided = replace(s, speed=0, trailer_damage=replace(s.trailer_damage, body=0.2))
    assert kinds(detector.feed(collided)) == ["collision"]
    now[0] = 7
    assert detector.feed(collided) == []


def test_jam_recovery_requires_thirty_seconds(timed_detector):
    detector, now = timed_detector
    s = jam_snapshot()
    assert jam_events(detector, now, s, 0, 90) == []
    assert jam_events(detector, now, replace(s, speed=70), 91, 121) == []
    assert jam_events(detector, now, s, 122, 240) == []
    assert len(jam_events(detector, now, s, 241, 242)) == 1


def test_jam_pause_resets_window(timed_detector):
    detector, now = timed_detector
    s = jam_snapshot()
    assert jam_events(detector, now, s, 0, 119) == []
    now[0] = 120
    detector.feed(replace(s, paused=True))
    assert jam_events(detector, now, s, 121, 240) == []
    assert len(jam_events(detector, now, s, 241, 241)) == 1


def test_collision_threshold_with_existing_wear(timed_detector):
    detector, now = timed_detector
    detector.feed(damaged_snapshot(slots=(0,), body=0.1))
    now[0] = 1
    events = detector.feed(damaged_snapshot(slots=(0,), body=0.105))
    assert kinds(events) == ["collision"]
    assert events[0].data["damage_increase_percent"] == pytest.approx(0.5)


def solar_snapshot(clock, **changes):
    return Snapshot(connected=True, x=49, game_time=f"0001-01-26T{clock}:00Z", **changes)


@pytest.mark.parametrize(
    "before,after,period,summary",
    [
        ("04:00", "05:00", "dawn", "Dawn is breaking."),
        ("05:00", "06:00", "day", "It's full daylight now."),
        ("20:00", "21:00", "dusk", "Dusk is falling."),
        ("21:30", "22:30", "night", "Night has fallen."),
    ],
)
def test_day_period_stable_change(detector, before, after, period, summary):
    from familiar_scs.geo import sun_elevation
    from familiar_scs.telemetry import game_hour

    s = solar_snapshot(before)
    assert "day_period" not in kinds(detector.feed(s))
    changed = solar_snapshot(after)
    assert "day_period" not in kinds(detector.feed(changed))
    assert "day_period" not in kinds(detector.feed(changed))
    event = next(e for e in detector.feed(changed) if e.kind == "day_period")
    assert event.priority == "ambient"
    assert event.data == {
        "period": period,
        "game_time": after,
        "sun_elevation": pytest.approx(sun_elevation(49, game_hour(changed.game_time))),
        "summary": summary,
    }
    assert "day_period" not in kinds(detector.feed(changed))


def test_day_period_candidate_must_be_consecutive(detector):
    day, dusk = solar_snapshot("20:00"), solar_snapshot("21:00")
    detector.feed(day)
    detector.feed(dusk)
    detector.feed(dusk)
    detector.feed(day)
    detector.feed(dusk)
    detector.feed(dusk)
    assert "day_period" in kinds(detector.feed(dusk))


@pytest.mark.parametrize("target", ["05:00", "23:00"])
def test_day_period_clock_jump_is_silent(detector, target):
    detector.feed(solar_snapshot("13:00", rest_remaining=timedelta(minutes=10)))
    jumped = solar_snapshot(target, rest_remaining=timedelta(hours=11))
    events = detector.feed(jumped)
    assert "rested" in kinds(events)
    assert "day_period" not in kinds(events)
    for _ in range(4):
        assert "day_period" not in kinds(detector.feed(jumped))


def test_day_period_midnight_rollover(detector):
    detector.feed(solar_snapshot("23:59"))
    s = replace(solar_snapshot("00:01"), game_time="0001-01-27T00:01:00Z")
    for _ in range(3):
        assert "day_period" not in kinds(detector.feed(s))


@pytest.mark.parametrize("changes", [{"paused": True}, {"connected": False}])
def test_day_period_inactive_and_resume(detector, changes):
    detector.feed(solar_snapshot("20:00"))
    detector.feed(solar_snapshot("21:00"))
    inactive = replace(solar_snapshot("23:00"), **changes)
    assert "day_period" not in kinds(detector.feed(inactive))
    for _ in range(4):
        assert "day_period" not in kinds(detector.feed(solar_snapshot("23:00")))


def test_day_period_invalid_time_resets_confirmation(detector):
    detector.feed(solar_snapshot("20:00"))
    dusk = solar_snapshot("21:00")
    detector.feed(dusk)
    detector.feed(dusk)
    assert "day_period" not in kinds(detector.feed(replace(dusk, game_time="invalid")))
    detector.feed(dusk)
    detector.feed(dusk)
    assert "day_period" in kinds(detector.feed(dusk))


@pytest.mark.parametrize("duration,stopped", [(59.99, False), (60, True)])
def test_rain_edges_and_minimum_duration(timed_detector, duration, stopped):
    detector, now = timed_detector
    dry = Snapshot(connected=True)
    detector.feed(dry)
    now[0] = 1
    wet = replace(dry, wipers_on=True)
    event = next(e for e in detector.feed(wet) if e.kind == "rain_started")
    assert event.priority == "ambient"
    assert event.data["summary"] == "Wipers switched on; it has probably started raining."
    assert "rain_started" not in kinds(detector.feed(wet))
    now[0] = 1 + duration
    events = detector.feed(dry)
    assert ("rain_stopped" in kinds(events)) is stopped
    assert all(e.priority == "ambient" for e in events)


def test_rain_independent_cooldowns(timed_detector):
    detector, now = timed_detector
    dry = Snapshot(connected=True)
    wet = replace(dry, wipers_on=True)
    detector.feed(dry)
    for timestamp, s, expected in [
        (1, wet, "rain_started"),
        (61, dry, "rain_stopped"),
        (62, wet, None),
        (122, dry, None),
        (301, wet, "rain_started"),
        (361, dry, "rain_stopped"),
    ]:
        now[0] = timestamp
        rain = [e.kind for e in detector.feed(s) if e.kind.startswith("rain_")]
        assert rain == ([expected] if expected else [])


@pytest.mark.parametrize("changes", [{"paused": True}, {"connected": False}])
def test_rain_inactive_snapshots(timed_detector, changes):
    detector, now = timed_detector
    dry = Snapshot(connected=True)
    wet = replace(dry, wipers_on=True)
    detector.feed(dry)
    now[0] = 1
    assert not {"rain_started", "rain_stopped"}.intersection(
        kinds(detector.feed(replace(wet, **changes)))
    )
    now[0] = 100
    assert "rain_started" not in kinds(detector.feed(wet))
    now[0] = 159
    assert "rain_stopped" not in kinds(detector.feed(dry))


def test_rain_first_connected_snapshot(timed_detector):
    detector, now = timed_detector
    wet = Snapshot(connected=True, wipers_on=True)
    assert "rain_started" not in kinds(detector.feed(wet))
    now[0] = 60
    assert "rain_stopped" in kinds(detector.feed(replace(wet, wipers_on=False)))
