import asyncio
import json
import threading
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace as Ns

import pytest
import yaml
from familiar.interfaces import ConfigError
from familiar_ksp.adapter import KspAdapter
from familiar_ksp.commands import KspProbe, KspRecord
from familiar_ksp.events import EventDetector
from familiar_ksp.quotes import attach_flavor, load_quotes
from familiar_ksp.telemetry import KrpcTelemetry, ReplayTelemetry, Snapshot, read_snapshot
from familiar_ksp.units import distance


def flight(**changes):
    return replace(
        Snapshot(
            connected=True,
            scene="flight",
            vessel_id="1",
            vessel_name="Explorer",
            vessel_type="ship",
            situation="flying",
            body="Kerbin",
            biome="Grasslands",
            altitude_m=1000,
            current_stage=3,
            has_atmosphere=True,
            atmosphere_depth_m=70000,
            periapsis_alt_m=-100,
            eccentricity=0.5,
            warp_rate=1,
            electric_charge=100,
            electric_charge_max=100,
            stage_liquid_fuel=100,
            stage_liquid_fuel_max=100,
            liquid_fuel=100,
            liquid_fuel_max=100,
        ),
        **changes,
    )


@pytest.fixture
def detector():
    now = [0.0]
    return EventDetector(lambda: now[0]), now


def kinds(events):
    return [e.kind for e in events]


def test_mapping():
    resources = Ns(amount=lambda name: 12, max=lambda name: 24)
    frames = []
    stages = []
    vessel = Ns(
        _object_id=42,
        name="Explorer",
        type=Ns(name="ship"),
        situation=Ns(name="sub_orbital"),
        biome="Shores",
        control=Ns(current_stage=3),
        crew=[Ns(name="Valentina Kerman")],
        crew_capacity=2,
        parts=Ns(all=[1, 2]),
        met=42,
        resources=resources,
        orbit=Ns(
            body=Ns(
                name="Kerbin", atmosphere_depth=70000, has_atmosphere=True, reference_frame="kerbin"
            ),
            speed=2300,
            inclination=3.141592653589793 / 2,
            eccentricity=0.4,
            apoapsis_altitude=90000,
            periapsis_altitude=80000,
            time_to_apoapsis=12,
            time_to_periapsis=36,
        ),
    )

    def read_flight(*args):
        frames.append(args)
        return Ns(
            mean_altitude=10000,
            surface_altitude=9000,
            speed=2000,
            vertical_speed=10,
            g_force=1.2,
            dynamic_pressure=123,
        )

    def decouple(number, tanks):
        def read(cumulative):
            stages.append(number)
            return tanks

        return Ns(number=number, resources=read)

    empty = Ns(amount=lambda name: 0, max=lambda name: 0)
    vessel.flight = read_flight
    vessel.decouple_stages = [decouple(-1, resources), decouple(1, resources), decouple(2, empty)]
    vessel.stages = [Ns(number=2, delta_v=0), Ns(number=1, delta_v=900), Ns(number=0, delta_v=0)]
    connection = Ns(
        krpc=Ns(game_scene=Ns(name="flight"), paused=False),
        space_center=Ns(active_vessel=vessel, ut=123, warp_rate=1, warp_mode=Ns(name="none")),
    )
    s = read_snapshot(connection)
    assert s.vessel_id == "42" and s.situation == "sub_orbital"
    assert s.inclination_deg == pytest.approx(90)
    assert (s.altitude_m, s.surface_altitude_m, s.speed_surface_ms, s.speed_orbital_ms) == (
        10000,
        9000,
        2000,
        2300,
    )
    assert s.dynamic_pressure_pa == 123 and s.crew == ("Valentina Kerman",)
    assert s.stage_monoprop == 12 and s.stage_monoprop_max == 24
    assert len(frames) == 1 and len(frames[0]) == 1 and stages == [2, 1]
    assert (s.stage_delta_v_ms, s.total_delta_v_ms) == (900, 900)
    with pytest.raises(FrozenInstanceError):
        s.altitude_m = 0
    del vessel.orbit.inclination
    assert read_snapshot(connection).inclination_deg is None
    connection.space_center.active_vessel = None
    assert not read_snapshot(connection).in_flight
    connection.krpc.game_scene.name = "editor_vab"
    del connection.space_center
    assert read_snapshot(connection).connected


@pytest.mark.parametrize(
    "changes", [{"connected": False}, {"scene": "editor_vab"}, {"vessel_id": "2"}, {"paused": True}]
)
def test_suppression(detector, changes):
    d, now = detector
    s = flight(situation="pre_launch")
    assert d.feed(s) == []
    assert d.feed(replace(s, **changes)) == []
    assert d.feed(flight(altitude_m=2000, current_stage=1)) == []
    now[0] = 20
    assert d.feed(flight(altitude_m=2000, current_stage=1)) == []


def test_liftoff_staging_burst(detector):
    d, now = detector
    d.feed(flight(situation="pre_launch"))
    # The launch staging (3 -> 2) is the liftoff itself, not a separate call.
    assert kinds(d.feed(flight(altitude_m=1100, current_stage=2))) == ["liftoff"]
    now[0] = 5
    assert kinds(d.feed(flight(current_stage=2))) == []
    now[0] = 10
    assert d.feed(flight(current_stage=1)) == []
    now[0] = 10.5
    assert d.feed(flight(current_stage=0)) == []
    now[0] = 11.4
    assert d.feed(flight(current_stage=0)) == []
    now[0] = 11.5
    e = d.feed(flight(current_stage=0))[0]
    assert e.kind == "staged" and e.data["from_stage"] == 2 and e.data["current_stage"] == 0
    assert e.data["ttl_s"] > 0 and 0 <= e.data["memory_salience"] <= 1


@pytest.mark.parametrize("air,pe", [(True, 70001), (False, 5001)])
def test_orbit_confirmation(detector, air, pe):
    d, now = detector
    d.feed(flight())
    s = flight(has_atmosphere=air, periapsis_alt_m=pe)
    assert d.feed(s) == []
    now[0] = 2.99
    assert d.feed(s) == []
    now[0] = 3
    e = d.feed(s)[0]
    assert e.kind == "orbit_reached" and e.data["memory_arrival"]
    now[0] = 100
    assert d.feed(s) == []


def test_initial_orbit_silent(detector):
    d, now = detector
    s = flight(periapsis_alt_m=80000)
    assert d.feed(s) == []
    now[0] = 100
    assert d.feed(s) == []


def test_soi_and_reentry(detector):
    d, now = detector
    d.feed(flight(altitude_m=71000, speed_orbital_ms=2200))
    e = d.feed(flight(altitude_m=69000, speed_orbital_ms=2100, vertical_speed_ms=-200))[0]
    assert e.kind == "reentry"
    e = d.feed(flight(body="Mun", has_atmosphere=False))[0]
    assert e.kind == "soi_changed" and e.data["memory_arrival"]
    assert d.feed(flight(body="Minmus", vessel_id="2")) == []


@pytest.mark.parametrize("situation", ["landed", "splashed"])
def test_landing_stability(detector, situation):
    d, now = detector
    d.feed(flight())
    s = flight(situation=situation)
    assert d.feed(s) == []
    now[0] = 1
    d.feed(flight())
    now[0] = 2
    assert d.feed(s) == []
    now[0] = 4
    e = d.feed(s)[0]
    assert e.kind == situation and e.data["biome"] == "Grasslands"
    assert e.data["memory_arrival"] and "Kerbin" in e.data["summary"]
    now[0] = 100
    assert d.feed(s) == []


def test_eva_switch_exception(detector):
    d, now = detector
    d.feed(flight())
    eva = flight(vessel_id="2", vessel_type="eva", vessel_name="Jebediah Kerman")
    e = d.feed(eva)[0]
    assert e.kind == "eva_started" and e.priority == "ambient"
    assert "Jebediah Kerman" in e.data["summary"]
    assert d.feed(eva) == []
    d.feed(Snapshot())
    assert d.feed(eva) == []


def test_warp_hold_cooldown(detector):
    d, now = detector
    d.feed(flight())
    assert d.feed(flight(warp_rate=10)) == []
    now[0] = 3
    assert kinds(d.feed(flight(warp_rate=10))) == ["warp_changed"]
    now[0] = 4
    assert d.feed(flight(warp_rate=1)) == []
    now[0] = 7
    assert d.feed(flight(warp_rate=1)) == []


@pytest.mark.parametrize(
    "field,kind", [("electric_charge", "low_charge"), ("liquid_fuel", "low_fuel")]
)
def test_resource_hysteresis(detector, field, kind):
    d, now = detector
    s = flight()
    d.feed(s)

    def update(value):
        return [e for e in d.feed(replace(s, **{field: value})) if e.kind == kind]

    assert update(20)[0].priority == "notable"
    assert update(21) == []
    assert update(19) == []
    assert update(5)[0].priority == "critical"
    assert update(6) == []
    assert update(4) == []
    assert update(11) == []
    assert update(5)[0].priority == "critical"
    assert update(26) == []
    assert update(20)[0].priority == "notable"
    assert d.feed(replace(s, situation="landed", **{field: 0})) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"electric_charge_max": 0},
        {"liquid_fuel_max": None},
        {"situation": "landed"},
        {"paused": True},
    ],
)
def test_no_spurious_warnings(detector, changes):
    d, now = detector
    s = flight(**changes)
    d.feed(s)
    es = d.feed(replace(s, electric_charge=0, liquid_fuel=0))
    excluded = {"low_charge", "low_fuel"}
    if "electric_charge_max" in changes:
        excluded = {"low_charge"}
    if "liquid_fuel_max" in changes:
        excluded = {"low_fuel"}
    assert not excluded.intersection(kinds(es))


@pytest.mark.asyncio
async def test_replay_and_adapter(tmp_path, monkeypatch):
    path = tmp_path / "flight.jsonl"
    s = flight(crew=("Valentina",), apoapsis_alt_m=80000)
    path.write_text(
        "\n".join(
            json.dumps(dict(ts=i, snapshot=item.to_dict()))
            for i, item in enumerate([s, replace(s, body="Mun")])
        )
    )
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    source = ReplayTelemetry(path, speed=2)
    assert [item async for item in source] == [s, replace(s, body="Mun")]
    assert sleeps == [0.5]
    adapter = KspAdapter({"replay": str(path), "replay_speed": 2})
    assert adapter.describe() == "" and adapter.whereabouts() is None
    events = [e async for e in adapter.events()]
    assert kinds(events) == ["soi_changed"]
    assert "Vessel: Explorer" in adapter.describe() and "80.0 km" in adapter.describe()
    assert "Valentina" in adapter.describe() and "Stage:" in adapter.describe()
    assert adapter.whereabouts() == dict(
        place_key="Mun/Grasslands",
        place="Grasslands",
        region="Mun",
        activity="flying the Explorer with Valentina",
    )
    result = await adapter.snapshot()
    assert result["driving"] and result["paused"] is False and result["crew"] == ["Valentina"]
    adapter.latest = Snapshot(connected=True, scene="space_center")
    assert adapter.describe() == "Not in flight" and adapter.whereabouts() is None
    await adapter.aclose()
    assert distance(10000) == "10000.0 m" and distance(10001) == "10.0 km"


@pytest.mark.asyncio
async def test_bad_replay(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"ts": NaN}')
    with pytest.raises(ConfigError, match="invalid replay"):
        _ = [s async for s in ReplayTelemetry(path)]
    with pytest.raises(ConfigError):
        ReplayTelemetry(path, 0)


def quote():
    return dict(
        id="test-quote",
        text="A fixture quote",
        speaker="Test speaker",
        context="Fixture, 2026",
        attribution="attributed",
        triggers=["liftoff"],
        source_url="https://example.org/source",
    )


def test_quotes(tmp_path, detector):
    packaged = load_quotes()
    assert packaged and all(q["triggers"] for q in packaged)
    path = tmp_path / "quotes.yaml"
    path.write_text(yaml.safe_dump([quote()]))
    d, now = detector
    d.feed(flight(situation="pre_launch"))
    e = d.feed(flight(altitude_m=1100))[0]
    attach_flavor(e, load_quotes(path))
    assert e.data["flavor"] == [
        {k: v for k, v in quote().items() if k not in {"triggers", "source_url"}}
    ]
    e.data.pop("flavor")
    attach_flavor(e, [{**quote(), "triggers": ["landed"]}])
    assert "flavor" not in e.data


@pytest.mark.parametrize(
    "change",
    [
        {"id": "Bad ID"},
        {"attribution": "fake"},
        {"text": ""},
        {"triggers": ["crash"]},
        {"triggers": "liftoff"},
        {"speaker": 1},
        {"source_url": "invalid"},
    ],
)
def test_invalid_quotes(tmp_path, change):
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump([{**quote(), **change}]))
    with pytest.raises(ValueError):
        load_quotes(path)


def test_duplicate_quotes(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump([quote(), quote()]))
    with pytest.raises(ValueError, match="duplicate"):
        load_quotes(path)


@pytest.mark.asyncio
async def test_worker_reconnect_and_ownership():
    threads = []
    calls = []

    class Connection:
        krpc = Ns(game_scene=Ns(name="editor_vab"), paused=False)

        def close(self):
            threads.append(threading.get_ident())

    connection = Connection()

    def connect(**kwargs):
        threads.append(threading.get_ident())
        calls.append(kwargs)
        if len(calls) == 1:
            raise OSError("offline fixture")
        return connection

    ticks = []

    async def heartbeat():
        # Exercise event-loop responsiveness while the dedicated worker owns RPC.
        # A timer also keeps restricted runners from relying on socketpair wakeups.
        while True:
            ticks.append(True)
            await asyncio.sleep(0.001)

    task = asyncio.create_task(heartbeat())
    source = KrpcTelemetry(connect=connect)
    try:
        assert not (await asyncio.wait_for(source.poll(), 2)).connected
        assert source.last_error == "offline fixture"
        assert (await asyncio.wait_for(source.poll(), 2)).connected
        assert calls[0]["name"] == "Familiar" and calls[0]["stream_port"] is None
        assert calls[0]["timeout"] > 0
        connection.krpc = None
        assert not (await asyncio.wait_for(source.poll(), 2)).connected
    finally:
        await asyncio.wait_for(source.aclose(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await source.aclose()
    assert ticks
    assert len(set(threads)) == 1 and threads[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_probe_without_server(monkeypatch, capsys):
    import familiar_ksp.commands as commands

    class Source:
        last_error = "fixture refused"

        def __init__(self, settings):
            pass

        async def poll(self):
            return Snapshot()

        async def aclose(self):
            pass

    monkeypatch.setattr(commands, "KrpcTelemetry", Source)
    assert await KspProbe().run(Ns(), Ns(games={})) == 1
    assert "KSP kRPC server unreachable: fixture refused" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_record(tmp_path, monkeypatch):
    import familiar_ksp.commands as commands

    class Source:
        def __init__(self, settings):
            pass

        async def __aiter__(self):
            yield flight()

        async def aclose(self):
            pass

    monkeypatch.setattr(commands, "KrpcTelemetry", Source)
    path = tmp_path / "record.jsonl"
    assert await KspRecord().run(Ns(file=path), Ns(games={})) == 0
    assert [s async for s in ReplayTelemetry(path)] == [flight()]


@pytest.mark.asyncio
async def test_disconnect_backoff_is_capped_and_resets(monkeypatch):
    import familiar_ksp.telemetry as telemetry

    sleeps = []
    snapshots = iter([Snapshot()] * 8 + [flight(), Snapshot(), Snapshot()])
    source = KrpcTelemetry()

    async def poll():
        return next(snapshots)

    async def sleep(delay):
        sleeps.append(delay)

    async def close():
        source.closed = True
        source.worker.shutdown(wait=False)

    monkeypatch.setattr(source, "poll", poll)
    monkeypatch.setattr(source, "aclose", close)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(telemetry.random, "uniform", lambda low, high: 1.0)
    iterator = source.__aiter__()
    try:
        for _ in range(11):
            await anext(iterator)
    finally:
        await iterator.aclose()
    assert sleeps[:7] == [0.5, 1, 2, 4, 8, 10, 10]
    assert max(sleeps) == 10
    assert sleeps[-2:] == [0.5, 0.5]
    assert source.closed


def test_interrupted_orbit_and_non_reentry(detector):
    d, now = detector
    d.feed(flight())
    orbit = flight(periapsis_alt_m=80000)
    d.feed(orbit)
    now[0] = 2
    assert d.feed(flight(eccentricity=1.1)) == []
    now[0] = 4
    assert d.feed(orbit) == []
    now[0] = 6
    assert d.feed(orbit) == []
    now[0] = 7
    assert kinds(d.feed(orbit)) == ["orbit_reached"]
    d.feed(flight(altitude_m=80000, speed_orbital_ms=100))
    assert "reentry" not in kinds(d.feed(flight(altitude_m=60000, vertical_speed_ms=-20)))


@pytest.mark.asyncio
async def test_probe_success(monkeypatch, capsys):
    import familiar_ksp.commands as commands

    closed = []

    class Source:
        def __init__(self, settings):
            pass

        async def poll(self):
            return flight()

        async def aclose(self):
            closed.append(True)

    monkeypatch.setattr(commands, "KrpcTelemetry", Source)
    assert await KspProbe().run(Ns(), Ns(games={})) == 0
    output = capsys.readouterr().out
    assert "Vessel: Explorer" in output
    assert json.loads(output.splitlines()[-1])["vessel_name"] == "Explorer"
    assert closed
