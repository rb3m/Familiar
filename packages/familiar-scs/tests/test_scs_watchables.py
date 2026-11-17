"""Both SCS packs expose navigation distance, percentages and SI speeds."""

from dataclasses import replace
from datetime import timedelta

import pytest
from familiar.interfaces import Watchable
from familiar_scs.adapter import AtsAdapter, Ets2Adapter
from familiar_scs.telemetry import Snapshot


@pytest.fixture(params=[AtsAdapter, Ets2Adapter])
def adapter(request):
    value = request.param.__new__(request.param)
    value._watch_timestamp = 123.0
    value.latest = Snapshot(
        connected=True,
        on_job=True,
        source_city="A",
        destination_city="B",
        source_company="Depot",
        destination_company="Shop",
        speed=72,
        speed_limit=54,
        nav_distance_km=50,
        nav_estimated=timedelta(hours=1),
        fuel=100,
        fuel_capacity=400,
        fuel_range=500,
        job_remaining=timedelta(hours=2),
        rest_remaining=timedelta(minutes=30),
    )
    return value


def test_quantities_samples_and_context(adapter):
    assert isinstance(adapter, Watchable)
    specs = adapter.watchables()
    expected = {
        "route_distance": (50000, "m"),
        "speed": (20, "m/s"),
        "fuel": (25, "%"),
        "fuel_range": (500000, "m"),
        "deadline_remaining": (7200, "s"),
        "rest_remaining": (1800, "s"),
        "speed_over_limit": (5, "m/s"),
    }
    sample = adapter.watch_sample()
    assert sample.values == {key: item[0] for key, item in expected.items()}
    assert {key: spec.unit for key, spec in specs.items()} == {
        key: item[1] for key, item in expected.items()
    }
    assert "remaining navigation" in specs["route_distance"].description
    assert "not straight-line" in specs["route_distance"].description
    assert "game time" in specs["deadline_remaining"].description
    assert sample.context == "A (Depot)->B (Shop)"
    assert sample.valid and sample.predictable
    assert sample.timestamp == 123
    assert adapter.watch_sample().timestamp == 123
    adapter.latest = replace(adapter.latest, destination_city="C")
    assert adapter.watch_sample().context == "A (Depot)->C (Shop)"


def test_free_roam_unknown_quantities_and_validity(adapter):
    adapter.latest = Snapshot(connected=True)
    sample = adapter.watch_sample()
    assert sample.context == "free roam"
    assert sample.values == {
        "route_distance": None,
        "speed": 0,
        "fuel": None,
        "fuel_range": None,
        "deadline_remaining": None,
        "rest_remaining": None,
        "speed_over_limit": None,
    }
    adapter.latest = replace(adapter.latest, nav_distance_km=5)
    assert adapter.watch_sample().values["route_distance"] == 5000
    adapter.latest = replace(adapter.latest, paused=True)
    assert not adapter.watch_sample().valid
    adapter.latest = Snapshot()
    assert not adapter.watch_sample().valid
