"""KSP watch quantities use SI, nullable resources and vessel identity."""

from dataclasses import replace

import pytest
from familiar.interfaces import Watchable
from familiar_ksp.adapter import KspAdapter
from familiar_ksp.telemetry import Snapshot


def adapter():
    # No RPC client is needed to describe cached telemetry.
    value = KspAdapter.__new__(KspAdapter)
    value._watch_timestamp = 123.0
    value.latest = Snapshot(
        connected=True,
        scene="flight",
        vessel_id="v1",
        vessel_name="Rocket",
        paused=False,
        warp_rate=1,
        altitude_m=4000,
        surface_altitude_m=3000,
        apoapsis_alt_m=100000,
        periapsis_alt_m=80000,
        navball_speed_ms=123,
        speed_surface_ms=100,
        speed_orbital_ms=200,
        vertical_speed_ms=-12,
        time_to_apoapsis_s=20,
        time_to_periapsis_s=40,
        target_distance_m=2000,
        target_speed_ms=5,
        liquid_fuel=10,
        liquid_fuel_max=40,
        electric_charge=90,
        electric_charge_max=100,
        stage_delta_v_ms=1500,
        met_s=60,
        stage_propellant_fraction=0.5,
    )
    return value


def test_all_quantities_and_units():
    value = adapter()
    assert isinstance(value, Watchable)
    specs = value.watchables()
    expected = {
        "altitude_sea_level": (4000, "m"),
        "altitude_terrain": (3000, "m"),
        "apoapsis": (100000, "m"),
        "periapsis": (80000, "m"),
        "navball_speed": (123, "m/s"),
        "surface_speed": (100, "m/s"),
        "orbital_speed": (200, "m/s"),
        "vertical_speed": (-12, "m/s"),
        "time_to_apoapsis": (20, "s"),
        "time_to_periapsis": (40, "s"),
        "target_distance": (2000, "m"),
        "target_speed": (5, "m/s"),
        "liquid_fuel": (25, "%"),
        "electric_charge": (90, "%"),
        "stage_delta_v": (1500, "m/s"),
        "mission_time": (60, "s"),
        "stage_propellant": (50, "%"),
    }
    sample = value.watch_sample()
    assert sample.values == {key: item[0] for key, item in expected.items()}
    assert {key: spec.unit for key, spec in specs.items()} == {
        key: item[1] for key, item in expected.items()
    }
    assert all(spec.description for spec in specs.values())
    assert sample.context == "v1"
    assert sample.valid and sample.predictable
    assert sample.timestamp == 123
    assert value.watch_sample().timestamp == 123  # cached polls keep acquisition time


@pytest.mark.parametrize(
    "changes",
    [
        {"connected": False},
        {"scene": "space_center"},
        {"paused": True},
        {"paused": None},
    ],
)
def test_validity(changes):
    value = adapter()
    value.latest = replace(value.latest, **changes)
    assert not value.watch_sample().valid


def test_warp_resources_and_unavailable_target():
    value = adapter()
    value.latest = replace(
        value.latest,
        warp_rate=10,
        liquid_fuel_max=0,
        electric_charge=None,
        target_distance_m=None,
        target_speed_ms=None,
    )
    sample = value.watch_sample()
    assert sample.valid and not sample.predictable
    assert sample.values["liquid_fuel"] is None
    assert sample.values["electric_charge"] is None
    assert sample.values["target_distance"] is None
    assert sample.values["target_speed"] is None
    value.latest = replace(value.latest, vessel_id="v2")
    assert value.watch_sample().context == "v2"
