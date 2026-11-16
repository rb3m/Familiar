import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from familiar.interfaces import ConfigError
from familiar_scs.telemetry import HttpTelemetry, ReplayTelemetry, Snapshot, parse_duration


def test_snapshot():
    s = Snapshot.from_json(
        {
            "game": {
                "connected": True,
                "paused": False,
                "time": "0001-01-26T12:00:00Z",
                "nextRestStopTime": "0001-01-26T18:35:00Z",
            },
            "truck": {
                "speed": 100,
                "fuel": 144,
                "fuelCapacity": 400,
                "fuelWarningOn": True,
                "placement": {"x": -42104.4, "z": -29011.4},
            },
            "navigation": {
                "estimatedDistance": 240000,
                "estimatedTime": "0001-01-01T04:10:00",
                "speedLimit": 105,
            },
            "job": {"remainingTime": "0001-01-02T01:00:00"},
        }
    )
    assert s.driving and s.fuel_warning
    assert s.speed == 100 and s.speed_limit == 105
    assert s.fuel_fraction == 0.36
    assert s.nav_distance_km == 240
    assert s.job_remaining == timedelta(hours=25)
    assert s.nav_estimated == timedelta(hours=4, minutes=10)
    assert s.rest_remaining == timedelta(hours=6, minutes=35)  # absolute time minus now
    assert s.x == -42104.4
    assert not Snapshot(connected=True, paused=True, speed=100).driving
    assert not Snapshot(connected=True, speed=3).driving
    assert Snapshot(fuel=4, fuel_capacity=2).fuel_fraction == 1
    assert Snapshot(fuel=-1, fuel_capacity=2).fuel_fraction == 0


@pytest.mark.parametrize("value", [None, "", "junk", "0001-01-01T00:00:00", 42])
def test_default_durations(value):
    assert parse_duration(value) is None


@pytest.mark.parametrize(
    "payload", [None, [], {}, {"game": {"connected": False}, "truck": {"speed": 100}}]
)
def test_disconnected(payload):
    assert Snapshot.from_json(payload) == Snapshot()


def test_malformed_fields():
    s = Snapshot.from_json(
        {
            "game": {"connected": True, "paused": "false"},
            "truck": {
                "speed": "nan",
                "fuel": [],
                "fuelCapacity": "inf",
                "placement": [],
                "fuelWarningOn": "true",
            },
            "job": [],
            "gameplay": {"finedDetails": []},
        }
    )
    assert s.connected and not s.paused
    assert s.speed == 0 and s.fuel_fraction == 0 and not s.fuel_warning
    assert s.fined_details == {}


def test_http_error_recovery(monkeypatch):
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("familiar_scs.telemetry.asyncio.sleep", sleep)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls <= 6:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, json={"game": {"connected": True}, "truck": {"speed": 50}})

    async def run():
        source = HttpTelemetry(interval_s=1, transport=httpx.MockTransport(handler))
        iterator = aiter(source)
        try:
            for _ in range(6):
                assert not (await anext(iterator)).connected
            assert (await anext(iterator)).speed == 50
        finally:
            await iterator.aclose()
            await source.aclose()
        assert source.client.is_closed

    asyncio.run(run())
    assert sleeps == [1, 2, 4, 8, 10, 10]


@pytest.mark.parametrize("response", [httpx.Response(500), httpx.Response(200, text="bad json")])
def test_http_bad_responses(response):
    async def run():
        source = HttpTelemetry(transport=httpx.MockTransport(lambda _: response))
        iterator = aiter(source)
        try:
            assert await anext(iterator) == Snapshot()
        finally:
            await iterator.aclose()
            await source.aclose()

    asyncio.run(run())


def test_replay(tmp_path, monkeypatch):
    path = tmp_path / "replay.jsonl"
    path.write_text(
        "\n".join(
            json.dumps({"ts": ts, "telemetry": {"game": {"connected": connected}}})
            for ts, connected in [(10, True), (14, False), (16, True)]
        )
    )
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("familiar_scs.telemetry.asyncio.sleep", sleep)

    async def run():
        return [s.connected async for s in ReplayTelemetry(path, speed=2)]

    assert asyncio.run(run()) == [True, False, True]
    assert sleeps == [2, 1]
    with pytest.raises(ConfigError):
        ReplayTelemetry(path, 0)


@pytest.mark.parametrize(
    "content",
    [
        "{}",
        "bad",
        '{"ts": "nan", "telemetry": {}}',
        '{"ts": 2, "telemetry": {}}\n{"ts": 1, "telemetry": {}}',
    ],
)
def test_invalid_replay(tmp_path, content):
    path = tmp_path / "bad.jsonl"
    path.write_text(content)

    async def run():
        return [s async for s in ReplayTelemetry(path)]

    with pytest.raises(ConfigError, match="bad.jsonl"):
        asyncio.run(run())


@pytest.mark.parametrize(
    ("game", "expected"),
    [
        # A real drive: day 26 of the game calendar, rest due 6 h 35 min later (once read as 618 h).
        (
            {"time": "0001-01-26T12:00:00Z", "nextRestStopTime": "0001-01-26T18:35:00Z"},
            timedelta(hours=6, minutes=35),
        ),
        (
            {"time": "0001-01-26T12:00:00Z", "nextRestStopTime": "0001-01-25T22:00:00Z"},
            timedelta(0),
        ),  # overdue
        ({"time": "0001-01-26T12:00:00Z", "nextRestStopTime": "0001-01-01T00:00:00Z"}, None),
        ({"nextRestStopTime": "0001-01-26T18:35:00Z"}, None),  # no game clock
    ],
)
def test_rest_remaining_is_relative_to_game_clock(game, expected):
    s = Snapshot.from_json({"game": {"connected": True, **game}})
    assert s.rest_remaining == expected


def test_durations_are_spoken_words():
    # "6 h 35 min" was read aloud letter by letter.
    from familiar_scs.units import duration_text

    assert duration_text(timedelta(hours=6, minutes=35)) == "6 hours 35 minutes"
    assert duration_text(timedelta(hours=1, minutes=1)) == "1 hour 1 minute"
    assert duration_text(timedelta(hours=2)) == "2 hours"
    assert duration_text(timedelta(minutes=47)) == "47 minutes"


def test_attached_trailer_damage():
    s = Snapshot.from_json(
        {
            "game": {"connected": True},
            "trailers": [
                {
                    "attached": True,
                    "wearChassis": 0.01,
                    "wearBody": 0.02,
                    "wearWheels": 0.03,
                    "cargoDamage": 0.04,
                },
                {"attached": False, "wearBody": 0.9, "cargoDamage": 0.9},
                {"attached": True, "wearChassis": 0.02, "cargoDamage": 0.01},
            ],
        }
    )
    assert s.trailer_damage.slots == (0, 2)
    assert s.trailer_damage.chassis == pytest.approx(0.03)
    assert s.trailer_damage.body == 0.02
    assert s.trailer_damage.wheels == 0.03
    assert s.trailer_damage.cargo == pytest.approx(0.05)


@pytest.mark.parametrize("trailers", [None, {}, [None, {}, {"attached": "true"}]])
def test_missing_or_malformed_trailers(trailers):
    s = Snapshot.from_json({"game": {"connected": True}, "trailers": trailers})
    assert s.trailer_damage.slots == ()
    assert s.trailer_damage.cargo == 0


@pytest.mark.parametrize(
    "value,expected", [(True, True), (False, False), (None, False), ("true", False), (1, False)]
)
def test_wipers(value, expected):
    s = Snapshot.from_json({"game": {"connected": True}, "truck": {"wipersOn": value}})
    assert s.wipers_on is expected
    assert not Snapshot().wipers_on


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0001-01-26T21:15:00Z", 21.25),
        ("0001-01-26T00:00:00", 0),
        ("0001-01-26T01:30:30.500+02:00", 1.5 + 30.5 / 3600),
        ("junk", None),
        ("", None),
        (None, None),
    ],
)
def test_game_hour(value, expected):
    from familiar_scs.telemetry import game_hour

    if expected is None:
        assert game_hour(value) is None
        return
    assert game_hour(value) == pytest.approx(expected)
