import asyncio
import json
from datetime import timedelta

import pytest
from familiar.interfaces import GameAdapter
from familiar_scs.adapter import AtsAdapter, Ets2Adapter
from familiar_scs.places import Place, PlaceIndex
from familiar_scs.telemetry import Snapshot


@pytest.fixture
def places(monkeypatch):
    index = PlaceIndex(places=(Place("Kaycee", "WY", 43.7339, -106.3402, 20),))
    monkeypatch.setattr(AtsAdapter, "_place_index", lambda self: index)
    return index


@pytest.mark.parametrize(
    "cls,speed,volume",
    [
        (AtsAdapter, "62 miles per hour", "38 gallons"),
        (Ets2Adapter, "100 kilometers per hour", "144 liters"),
    ],
)
def test_describe(places, cls, speed, volume):
    adapter = cls({})
    assert isinstance(adapter, GameAdapter)
    assert adapter.describe() == ""
    adapter.latest = Snapshot(
        connected=True,
        paused=True,
        x=-42104.4,
        z=-29011.4,
        speed=100,
        speed_limit=105,
        fuel=144,
        fuel_capacity=400,
        fuel_range=500,
        on_job=True,
        cargo="potatoes",
        cargo_mass=18000,
        source_city="Boise",
        source_company="Potato Co",
        destination_city="Reno",
        destination_company="Market",
        nav_distance_km=240,
        nav_estimated=timedelta(hours=4, minutes=10),
        job_remaining=timedelta(hours=9, minutes=30),
        rest_remaining=timedelta(hours=3, minutes=20),
    )
    description = adapter.describe()
    assert speed in description and volume in description
    assert "Fuel: 36%" in description
    assert "potatoes from Boise (Potato Co) to Reno (Market)" in description
    assert "ETA 4 hours 10 minutes, deadline in 9 hours 30 minutes (game time)" in description
    assert "Rest needed in 3 hours 20 minutes (game time)" in description
    assert "Game paused" in description
    if cls is AtsAdapter:
        assert "near Kaycee, Wyoming" in description
    assert adapter.tools() == []
    snapshot = asyncio.run(adapter.snapshot())
    assert snapshot["connected"] and snapshot["paused"] and not snapshot["driving"]
    assert snapshot["lat"] is not None
    json.dumps(snapshot)
    asyncio.run(adapter.aclose())


def test_replay_adapter(places, tmp_path):
    path = tmp_path / "drive.jsonl"
    path.write_text(
        json.dumps(
            {
                "ts": 0,
                "telemetry": {
                    "game": {"connected": True},
                    "truck": {
                        "speed": 50,
                        "fuelWarningOn": True,
                        "placement": {"x": -42104.4, "z": -29011.4},
                    },
                },
            }
        )
        + "\n"
    )
    adapter = AtsAdapter({"replay": str(path)})

    async def run():
        before = await adapter.snapshot()
        assert before["lat"] is None and not before["connected"]
        events = [e async for e in adapter.events()]
        assert {e.kind for e in events} >= {"game_state", "fuel_low"}
        snapshot = await adapter.snapshot()
        assert snapshot["driving"]
        assert snapshot["state"] == "WY"
        assert snapshot["nearest_place"]["name"] == "Kaycee"
        await adapter.aclose()

    asyncio.run(run())


def test_ets2_packaged_places_and_country_crossing():
    adapter = Ets2Adapter({})
    germany = Snapshot(connected=True, x=0, z=0)
    france = Snapshot(connected=True, x=-26253.693, z=6106.096)
    for sample in (germany, france):
        events = [event for _ in range(3) for event in adapter.detector.feed(sample)]
    crossing = next(event for event in events if event.kind == "entered_state")
    assert crossing.data["summary"] == "Entered France from Germany."
    assert crossing.data["from_state"] == "Germany"
    assert crossing.data["to_state"] == "France"
    assert crossing.data["from_code"] == "DE"
    assert crossing.data["to_code"] == "FR"
    adapter.latest = france
    assert "near Château-Thierry, France" in adapter.describe()
    snapshot = asyncio.run(adapter.snapshot())
    assert snapshot["state"] == "FR"
    assert snapshot["nearest_place"]["name"] == "Château-Thierry"
    asyncio.run(adapter.aclose())


@pytest.mark.parametrize("cls", [AtsAdapter, Ets2Adapter])
@pytest.mark.parametrize(
    "game_time,expected",
    [
        ("0001-01-26T21:15:00Z", "Time: 21:15 in-game, dusk"),
        ("0001-01-26T05:01:00Z", "Time: 05:01 in-game, dawn"),
        ("junk", None),
        ("", None),
    ],
)
def test_describe_game_time(places, cls, game_time, expected):
    adapter = cls({})
    adapter.projection = lambda x, z: (49, 3)
    adapter.latest = Snapshot(connected=True, game_time=game_time)
    description = adapter.describe()
    if expected is None:
        assert "Time:" not in description
        asyncio.run(adapter.aclose())
        return
    assert expected in description
    asyncio.run(adapter.aclose())


@pytest.mark.parametrize(
    "cls,code,name,region",
    [
        (AtsAdapter, "WY", "Kaycee", "Wyoming"),
        (Ets2Adapter, "FR", "Château-Thierry", "France"),
    ],
)
async def test_memory_whereabouts(cls, code, name, region, monkeypatch):
    index = PlaceIndex(places=(Place(name, code, 49, 3, 20),))
    monkeypatch.setattr(cls, "_place_index", lambda self: index)
    adapter = cls({"units": "metric"})
    adapter.projection = lambda x, z: (49, 3)
    assert adapter.whereabouts() is None
    adapter.latest = Snapshot(connected=True)
    assert adapter.whereabouts() == {
        "place_key": f"{code}/{name}",
        "place": name,
        "region": region,
        "activity": "driving without a job",
    }
    adapter.latest = Snapshot(
        connected=True, on_job=True, cargo="cattle", cargo_mass=18000, destination_city="Bayonne"
    )
    assert adapter.whereabouts()["activity"] == "hauling 18 tonnes of cattle to Bayonne"
    adapter.places = PlaceIndex(places=())
    assert adapter.whereabouts() is None
    await adapter.aclose()


@pytest.mark.parametrize(
    "kind,salience,arrival",
    [
        ("passing_town", 0.1, True),
        ("entered_state", 0.2, True),
        ("landmark_nearby", 0.4, True),
        ("job_started", 0.5, False),
        ("job_delivered", 0.6, False),
        ("ferry", 0.5, False),
        ("train", 0.5, False),
        ("traffic_jam", 0.5, False),
        ("fined", 0.7, False),
        ("collision", 0.8, False),
        ("hard_braking", 0.4, False),
        ("rested", 0.3, False),
        ("rain_started", 0.2, False),
        ("fuel_low", 0, False),
        ("rain_stopped", 0, False),
        ("speeding", 0, False),
    ],
)
async def test_event_memory_metadata(places, kind, salience, arrival):
    adapter = AtsAdapter({})
    event = adapter.detector._event(kind, "notable", "A grounded fact.")
    assert event.data.get("memory_salience", 0) == salience
    assert event.data.get("memory_arrival", False) is arrival
    assert event.data["summary"] == "A grounded fact."
    await adapter.aclose()
