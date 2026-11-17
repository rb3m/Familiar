"""Tests for KSP resource configuration, runtime propellant discovery and caching."""

from types import SimpleNamespace as Ns

import pytest
import yaml
from familiar.interfaces import ConfigError, Watchable
from familiar_ksp.adapter import KspAdapter
from familiar_ksp.resources import display_name, load_resources, split_camel_case
from familiar_ksp.telemetry import (
    Snapshot,
    burning_resources,
    discover_propellants,
    read_snapshot,
)


def _mock_connection(vessel, scene="flight"):
    return Ns(
        krpc=Ns(game_scene=Ns(name=scene), paused=False),
        space_center=Ns(active_vessel=vessel, ut=100.0, warp_rate=1, warp_mode=Ns(name="none")),
    )


def test_resources_loading_defaults():
    cfg = load_resources()
    assert cfg.vessel["electric_charge"] == "ElectricCharge"
    assert cfg.vessel["liquid_fuel"] == "LiquidFuel"
    assert cfg.stage["stage_liquid_fuel"] == "LiquidFuel"
    assert cfg.stage["stage_oxidizer"] == "Oxidizer"
    assert cfg.stage["stage_solid_fuel"] == "SolidFuel"
    assert cfg.stage["stage_monoprop"] == "MonoPropellant"
    assert cfg.fallback_propellants == ("LiquidFuel", "Oxidizer", "SolidFuel")
    assert cfg.display_names["LiquidFuel"] == "liquid fuel"
    assert cfg.display_names["LqdHydrogen"] == "liquid hydrogen"
    assert cfg.display_names["LqdMethane"] == "liquid methane"
    assert display_name("LiquidFuel") == "liquid fuel"
    assert display_name("LqdHydrogen") == "liquid hydrogen"


def test_resources_user_override(tmp_path):
    user_file = tmp_path / "data" / "familiar_ksp" / "resources.yaml"
    user_file.parent.mkdir(parents=True, exist_ok=True)
    user_file.write_text(
        yaml.safe_dump(
            {
                "display_names": {
                    "LqdHydrogen": "deep space liquid hydrogen",
                    "CustomKerosene": "refined kerosene",
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = load_resources()
    assert cfg.display_names["CustomKerosene"] == "refined kerosene"
    assert cfg.display_names["LqdHydrogen"] == "deep space liquid hydrogen"
    assert cfg.display_names["LiquidFuel"] == "liquid fuel"  # merged with default
    assert display_name("CustomKerosene") == "refined kerosene"
    assert display_name("LqdHydrogen") == "deep space liquid hydrogen"


@pytest.mark.parametrize(
    "invalid_data",
    [
        {"vessel": "not_a_mapping"},
        {"vessel": {"liquid_fuel": None}},  # null removes key, missing liquid_fuel
        {"stage": [1, 2, 3]},
        {"fallback_propellants": []},
        {"fallback_propellants": "string"},
        {"display_names": None},
    ],
)
def test_resources_validation_error_names_file(tmp_path, invalid_data):
    user_file = tmp_path / "data" / "familiar_ksp" / "resources.yaml"
    user_file.parent.mkdir(parents=True, exist_ok=True)
    user_file.write_text(yaml.safe_dump(invalid_data), encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        load_resources()
    assert "resources.yaml" in str(exc_info.value)


def test_camel_case_splitting():
    assert split_camel_case("Simple") == "simple"
    assert split_camel_case("LiquidFuel") == "liquid fuel"
    assert split_camel_case("LqdHydrogen") == "lqd hydrogen"
    assert split_camel_case("SuperLOXTank") == "super lox tank"
    assert split_camel_case("") == ""
    # display_name falls back to split_camel_case for unknown names
    assert display_name("UnknownModFuel") == "unknown mod fuel"


def test_discovery_with_fake_engines_modded_fuels():
    engine = Ns(
        active=True,
        propellant_names=["LqdHydrogen", "Oxidizer"],
        part=Ns(stage=1),
    )
    tank_resources = Ns(
        amount=lambda name: {"LqdHydrogen": 40.0, "Oxidizer": 120.0}.get(name, 0.0),
        max=lambda name: {"LqdHydrogen": 100.0, "Oxidizer": 200.0}.get(name, 0.0),
    )
    decouple = Ns(number=1, resources=lambda cumulative: tank_resources)
    vessel = Ns(
        _object_id=99,
        name="CryoVessel",
        type=Ns(name="ship"),
        situation=Ns(name="flying"),
        biome="Shores",
        control=Ns(current_stage=2, speed_mode=Ns(name="surface")),
        crew=[],
        crew_capacity=1,
        parts=Ns(all=[1, 2, 3], engines=[engine]),
        met=10.0,
        resources=Ns(amount=lambda name: 100, max=lambda name: 100),
        orbit=Ns(
            body=Ns(name="Kerbin", atmosphere_depth=70000, has_atmosphere=True, reference_frame=1),
            speed=500,
            inclination=0,
            eccentricity=0.1,
            apoapsis_altitude=80000,
            periapsis_altitude=70000,
            time_to_apoapsis=100,
            time_to_periapsis=200,
        ),
        decouple_stages=[decouple],
        stages=[],
        flight=lambda ref: Ns(
            mean_altitude=1000,
            surface_altitude=900,
            speed=500,
            vertical_speed=50,
            g_force=1.0,
            dynamic_pressure=100,
        ),
    )
    connection = _mock_connection(vessel)
    snapshot = read_snapshot(connection)

    assert snapshot.stage_propellants == {
        "LqdHydrogen": {"amount": 40.0, "max": 100.0},
        "Oxidizer": {"amount": 120.0, "max": 200.0},
    }
    # min(40/100, 120/200) = min(0.40, 0.60) = 0.40
    assert snapshot.stage_propellant_fraction == pytest.approx(0.40)


def test_discovery_fallback_when_engines_report_nothing():
    engine = Ns(active=False, propellant_names=[], part=Ns(stage=-1))
    vessel = Ns(parts=Ns(engines=[engine]))
    fallback = ("LiquidFuel", "Oxidizer", "SolidFuel")
    discovered = discover_propellants(vessel, current_stage=2, fallback=fallback)
    assert discovered == fallback


def test_discovery_next_to_fire_when_inactive():
    # Pad scenario: engine not yet active, staged at stage 1 while current_stage is 2
    engine = Ns(
        active=False,
        propellant_names=["LqdHydrogen", "Oxidizer"],
        part=Ns(stage=1),
    )
    vessel = Ns(parts=Ns(engines=[engine]))
    discovered = discover_propellants(vessel, current_stage=2, fallback=("LiquidFuel", "Oxidizer"))
    assert discovered == ("LqdHydrogen", "Oxidizer")


def test_cache_refresh_on_stage_change():
    call_count = [0]

    class CountingEngine:
        def __init__(self, stage, names):
            self.active = False
            self.part = Ns(stage=stage)
            self._names = names

        @property
        def propellant_names(self):
            call_count[0] += 1
            return self._names

    engine_stage2 = CountingEngine(stage=2, names=["LiquidFuel", "Oxidizer"])
    engine_stage1 = CountingEngine(stage=1, names=["LqdHydrogen", "Oxidizer"])
    vessel = Ns(
        _object_id="vessel_cache_test",
        parts=Ns(engines=[engine_stage2, engine_stage1]),
    )

    cache: dict[tuple[str, int], tuple[str, ...]] = {}

    # Poll 1 (stage 3): next to fire is stage 2
    prop_stage3 = discover_propellants(vessel, current_stage=3, fallback=("LiquidFuel", "Oxidizer"))
    cache[("vessel_cache_test", 3)] = prop_stage3
    assert prop_stage3 == ("LiquidFuel", "Oxidizer")
    assert call_count[0] == 1

    # Poll 2 (stage 3): cache hit, discover_propellants not invoked
    assert cache[("vessel_cache_test", 3)] == ("LiquidFuel", "Oxidizer")
    assert call_count[0] == 1

    # Poll 3 (stage changed to 2): cache miss for stage 2, refresh occurs
    stale = [k for k in cache if k[0] == "vessel_cache_test" and k[1] != 2]
    for k in stale:
        cache.pop(k, None)
    prop_stage2 = discover_propellants(vessel, current_stage=2, fallback=("LiquidFuel", "Oxidizer"))
    cache[("vessel_cache_test", 2)] = prop_stage2
    assert prop_stage2 == ("LqdHydrogen", "Oxidizer")
    assert call_count[0] == 2
    assert ("vessel_cache_test", 3) not in cache
    assert ("vessel_cache_test", 2) in cache

    # Poll 4 (stage 2): cache hit
    assert cache[("vessel_cache_test", 2)] == ("LqdHydrogen", "Oxidizer")
    assert call_count[0] == 2


def test_new_watchable_value_and_units():
    adapter = KspAdapter.__new__(KspAdapter)
    adapter._watch_timestamp = 200.0
    adapter.latest = Snapshot(
        connected=True,
        scene="flight",
        vessel_id="v_watch",
        vessel_name="WatchTest",
        paused=False,
        warp_rate=1,
        altitude_m=5000,
        surface_altitude_m=4000,
        apoapsis_alt_m=90000,
        periapsis_alt_m=70000,
        navball_speed_ms=100,
        speed_surface_ms=100,
        speed_orbital_ms=500,
        vertical_speed_ms=10,
        time_to_apoapsis_s=50,
        time_to_periapsis_s=100,
        target_distance_m=1000,
        target_speed_ms=2,
        liquid_fuel=10,
        liquid_fuel_max=40,
        electric_charge=50,
        electric_charge_max=100,
        stage_delta_v_ms=1200,
        met_s=30,
        stage_propellant_fraction=0.35,
    )
    assert isinstance(adapter, Watchable)
    specs = adapter.watchables()
    assert specs["stage_propellant"].unit == "%"
    assert specs["stage_propellant"].unit == "%"
    assert specs["stage_propellant"].description
    sample = adapter.watch_sample()
    assert sample.values["stage_propellant"] == pytest.approx(35.0)
    assert sample.values["stage_propellant"] == pytest.approx(35.0)

    # When fraction is None
    adapter.latest = Snapshot(
        connected=True,
        scene="flight",
        vessel_id="v_watch",
        vessel_name="WatchTest",
        paused=False,
        warp_rate=1,
        stage_propellant_fraction=None,
    )
    sample_none = adapter.watch_sample()
    assert sample_none.values["stage_propellant"] is None
    assert sample_none.values["stage_propellant"] is None


def test_burning_resources_with_modded_fuels():
    tank_resources = Ns(
        amount=lambda name: 100.0,
        max=lambda name: 100.0 if name == "LqdHydrogen" else 0.0,
    )
    decouple = Ns(number=1, resources=lambda cumulative: tank_resources)
    vessel = Ns(decouple_stages=[decouple])

    # Stock propellants don't find the tank
    assert burning_resources(vessel, 2, ("LiquidFuel", "Oxidizer")) is None
    # Discovered modded propellant finds the tank
    assert burning_resources(vessel, 2, ("LqdHydrogen", "Oxidizer")) is tank_resources


def test_describe_names_discovered_modded_fuels():
    adapter = KspAdapter.__new__(KspAdapter)
    adapter.resources = load_resources()
    adapter.latest = Snapshot(
        connected=True,
        scene="flight",
        vessel_id="v1",
        vessel_name="Hydrolox",
        stage_propellants={
            "LqdHydrogen": {"amount": 50.0, "max": 100.0},
            "Oxidizer": {"amount": 20.0, "max": 40.0},
        },
    )
    text = adapter.describe()
    assert "liquid hydrogen" in text
    assert "oxidizer" in text
    assert "LqdHydrogen" not in text
