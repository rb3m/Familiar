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
    "cls,speed,volume", [(AtsAdapter, "62 mph", "38 gal"), (Ets2Adapter, "100 km/h", "144 L")]
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
    assert "ETA 4 h 10 min, deadline in 9 h 30 min (game time)" in description
    assert "Rest needed in: 3 h 20 min (game time)" in description
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
