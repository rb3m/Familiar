"""Polling and replay sources; all synchronous RPC work stays on one thread."""

import asyncio
import json
import math
import random
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Protocol

import krpc
from familiar.interfaces import ConfigError

from .resources import ResourceConfig, load_resources


def positive(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be positive and finite")
    return value


@dataclass(frozen=True)
class Snapshot:
    connected: bool = False
    scene: str | None = None
    vessel_id: str | None = None
    vessel_name: str | None = None
    vessel_type: str | None = None
    situation: str | None = None
    body: str | None = None
    biome: str | None = None
    altitude_m: float | None = None
    surface_altitude_m: float | None = None
    speed_orbital_ms: float | None = None
    speed_surface_ms: float | None = None
    vertical_speed_ms: float | None = None
    apoapsis_alt_m: float | None = None
    periapsis_alt_m: float | None = None
    time_to_apoapsis_s: float | None = None
    time_to_periapsis_s: float | None = None
    inclination_deg: float | None = None
    eccentricity: float | None = None
    atmosphere_depth_m: float | None = None
    has_atmosphere: bool | None = None
    dynamic_pressure_pa: float | None = None
    g_force: float | None = None
    current_stage: int | None = None
    part_count: int | None = None
    crew: tuple[str, ...] = ()
    crew_capacity: int | None = None
    met_s: float | None = None
    ut_s: float | None = None
    warp_rate: float | None = None
    warp_mode: str | None = None
    electric_charge: float | None = None
    electric_charge_max: float | None = None
    liquid_fuel: float | None = None  # whole vessel; the low-fuel warning uses this
    liquid_fuel_max: float | None = None
    stage_delta_v_ms: float | None = None
    speed_mode: str | None = None  # navball mode: orbit, surface or target
    navball_speed_ms: float | None = None  # the speed the navball shows in that mode
    target_name: str | None = None
    target_kind: str | None = None  # vessel, body or docking_port
    target_distance_m: float | None = None
    target_speed_ms: float | None = None  # relative speed
    total_delta_v_ms: float | None = None
    stage_liquid_fuel: float | None = None
    stage_liquid_fuel_max: float | None = None
    stage_oxidizer: float | None = None
    stage_oxidizer_max: float | None = None
    stage_solid_fuel: float | None = None
    stage_solid_fuel_max: float | None = None
    stage_monoprop: float | None = None
    stage_monoprop_max: float | None = None
    paused: bool | None = None
    stage_propellants: dict[str, dict[str, float | None]] = field(default_factory=dict)
    stage_propellant_fraction: float | None = None

    @property
    def in_flight(self) -> bool:
        return self.connected and self.scene == "flight" and self.vessel_name is not None

    @classmethod
    def from_dict(cls, data: dict) -> "Snapshot":
        values = {f.name: data[f.name] for f in fields(cls) if f.name in data}
        values["crew"] = tuple(values.get("crew", ()))
        return cls(**values)

    def to_dict(self) -> dict:
        result = asdict(self)
        result["crew"] = list(self.crew)
        return result


def optional(read: Callable):
    """Missing or scene-inapplicable members are unknown, never invented zeros."""
    try:
        value = read()
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    except (OSError, EOFError):
        raise  # connection loss: let the poll reconnect
    except Exception:  # kRPC maps server errors to ValueError, RuntimeError, RPCError...
        return None


def enum_name(value) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "name", value)).lower()


def resource_values(resources, prefix: str, name: str) -> dict:
    return {
        prefix: optional(lambda: resources.amount(name)),
        f"{prefix}_max": optional(lambda: resources.max(name)),
    }


def read_snapshot(
    connection,
    cache: dict[tuple[str, int], tuple[str, ...]] | None = None,
    resources_config: ResourceConfig | None = None,
) -> Snapshot:
    """Map the kRPC 0.7 object graph. Called only by the owning worker."""
    res_cfg = resources_config if resources_config is not None else load_resources()
    scene = enum_name(connection.krpc.game_scene)
    common = dict(connected=True, scene=scene, paused=optional(lambda: connection.krpc.paused))
    if scene != "flight":
        return Snapshot(**common)
    sc = connection.space_center
    # Reacquire every poll, including after docking or switches.
    vessel = optional(lambda: sc.active_vessel)
    if vessel is None:
        return Snapshot(**common)
    orbit = optional(lambda: vessel.orbit)
    body = optional(lambda: orbit.body)
    # The default frame moves with the vessel, so its speeds read 0; use the body's frame.
    flight = optional(lambda: vessel.flight(body.reference_frame))
    values = dict(common)
    values.update(
        vessel_id=str(getattr(vessel, "_object_id", vessel.name)),
        vessel_name=vessel.name,
        vessel_type=enum_name(optional(lambda: vessel.type)),
        situation=enum_name(optional(lambda: vessel.situation)),
        body=optional(lambda: body.name),
        biome=optional(lambda: vessel.biome),
        current_stage=optional(lambda: vessel.control.current_stage),
        part_count=optional(lambda: len(vessel.parts.all)),
        crew=optional(lambda: tuple(member.name for member in vessel.crew)) or (),
        crew_capacity=optional(lambda: vessel.crew_capacity),
        met_s=optional(lambda: vessel.met),
        ut_s=optional(lambda: sc.ut),
        warp_rate=optional(lambda: sc.warp_rate),
        warp_mode=enum_name(optional(lambda: sc.warp_mode)),
    )
    for key, obj, attr in (
        ("altitude_m", flight, "mean_altitude"),
        ("surface_altitude_m", flight, "surface_altitude"),
        ("speed_surface_ms", flight, "speed"),
        ("vertical_speed_ms", flight, "vertical_speed"),
        ("dynamic_pressure_pa", flight, "dynamic_pressure"),
        ("g_force", flight, "g_force"),
        ("speed_orbital_ms", orbit, "speed"),
        ("apoapsis_alt_m", orbit, "apoapsis_altitude"),
        ("periapsis_alt_m", orbit, "periapsis_altitude"),
        ("time_to_apoapsis_s", orbit, "time_to_apoapsis"),
        ("time_to_periapsis_s", orbit, "time_to_periapsis"),
        ("eccentricity", orbit, "eccentricity"),
        ("atmosphere_depth_m", body, "atmosphere_depth"),
        ("has_atmosphere", body, "has_atmosphere"),
    ):
        values[key] = optional(lambda obj=obj, attr=attr: getattr(obj, attr))
    inclination = optional(lambda: orbit.inclination)
    values["inclination_deg"] = math.degrees(inclination) if inclination is not None else None
    vessel_resources = optional(lambda: vessel.resources)
    for field_name, res_name in res_cfg.vessel.items():
        values.update(resource_values(vessel_resources, field_name, res_name))
    stage = values["current_stage"]
    vessel_id = values.get("vessel_id")

    cache_key = (vessel_id, stage) if vessel_id is not None and stage is not None else None
    if cache is not None and cache_key is not None and cache_key in cache:
        propellants = cache[cache_key]
    else:
        fallback = res_cfg.fallback_propellants
        propellants = discover_propellants(vessel, stage, fallback)
        if cache is not None and cache_key is not None:
            for k in [k for k in cache if k[0] == vessel_id and k[1] != stage]:
                cache.pop(k, None)
            cache[cache_key] = propellants

    resources = (
        optional(lambda: burning_resources(vessel, stage, propellants))
        if stage is not None
        else None
    )
    values.update(delta_v(vessel, stage))
    values.update(optional(lambda: target_values(sc, vessel, body)) or {})
    values.update(navball(optional(lambda: vessel.control.speed_mode), values))
    for key, name in res_cfg.stage.items():
        values.update(resource_values(resources, key, name))

    stage_propellants = {}
    fractions = []
    if resources is not None:
        for name in propellants:
            amt = optional(lambda name=name: resources.amount(name))
            cap = optional(lambda name=name: resources.max(name))
            stage_propellants[name] = {"amount": amt, "max": cap}
            if amt is not None and cap is not None and cap > 0:
                fractions.append(amt / cap)

    values["stage_propellants"] = stage_propellants
    values["stage_propellant_fraction"] = min(fractions) if fractions else None
    return Snapshot(**values)


def discover_propellants(
    vessel, current_stage: int | None, fallback: tuple[str, ...]
) -> tuple[str, ...]:
    engines = optional(lambda: vessel.parts.engines)
    if not engines:
        return fallback

    active = [e for e in engines if optional(lambda e=e: e.active)]
    if active:
        names = _extract_propellant_names(active)
        if names:
            return names

    if current_stage is None:
        return fallback

    candidates = _staged_engines(engines)
    if not candidates:
        return fallback

    future = [item for item in candidates if item[0] < current_stage]
    target_stages = future if future else [item for item in candidates if item[0] <= current_stage]
    if not target_stages:
        return fallback

    next_stage_num = max(stage_num for stage_num, _ in target_stages)
    next_engines = [e for stage_num, e in target_stages if stage_num == next_stage_num]
    names = _extract_propellant_names(next_engines)
    if names:
        return names

    return fallback


def _staged_engines(engines) -> list[tuple[int, object]]:
    result = []
    for engine in engines:
        part = optional(lambda e=engine: e.part)
        if part is None:
            continue
        stage_num = optional(lambda p=part: p.stage)
        if stage_num is not None and stage_num >= 0:
            result.append((stage_num, engine))
    return result


def _extract_propellant_names(engines) -> tuple[str, ...]:
    seen: list[str] = []
    for engine in engines:
        p_names = optional(lambda e=engine: e.propellant_names)
        if not p_names:
            continue
        for name in p_names:
            if name and name not in seen:
                seen.append(name)
    return tuple(seen)


def burning_resources(vessel, current_stage: int, propellants: tuple[str, ...] | list[str]):
    """Tanks feeding the next burn: the highest decouple stage below the current one with
    propellant (on the pad the engines' tanks sit two stage numbers down)."""
    for stage in sorted(vessel.decouple_stages, key=lambda item: item.number, reverse=True):
        if stage.number >= current_stage:
            continue
        resources = stage.resources(cumulative=False)
        if any(optional(lambda name=name, r=resources: r.max(name) > 0) for name in propellants):
            return resources
    return None


def delta_v(vessel, current_stage: int | None) -> dict:
    """Stock delta-v (current conditions): the next burning stage and the whole vessel."""
    stages = optional(lambda: [(item.number, item.delta_v) for item in vessel.stages]) or []
    burning = [
        dv
        for number, dv in sorted(stages, reverse=True)
        if dv > 0 and (current_stage is None or number <= current_stage)
    ]
    return {
        "stage_delta_v_ms": burning[0] if burning else None,
        "total_delta_v_ms": sum(dv for _, dv in stages) if stages else None,
    }


def _current_target(sc):
    """Most specific target first, matching what KSP's navball uses."""
    port = sc.target_docking_port
    if port is not None:
        return "docking_port", f"{port.part.vessel.name} docking port", port.part
    target = sc.target_vessel
    if target is not None:
        return "vessel", target.name, target
    target = sc.target_body
    if target is not None:
        return "body", target.name, target
    return None


def _norm(vector) -> float:
    return math.sqrt(sum(component * component for component in vector))


def target_values(sc, vessel, body) -> dict:
    found = _current_target(sc)
    if found is None:
        return {}
    kind, name, target = found
    frame = body.non_rotating_reference_frame
    offset = [a - b for a, b in zip(target.position(frame), vessel.position(frame), strict=True)]
    relative = [a - b for a, b in zip(target.velocity(frame), vessel.velocity(frame), strict=True)]
    return {
        "target_kind": kind,
        "target_name": name,
        "target_distance_m": _norm(offset),
        "target_speed_ms": _norm(relative),
    }


NAVBALL_SPEED = {
    "orbit": "speed_orbital_ms",
    "surface": "speed_surface_ms",
    "target": "target_speed_ms",
}


def navball(mode, values: dict) -> dict:
    name = enum_name(mode)
    field = NAVBALL_SPEED.get(name)
    return {"speed_mode": name, "navball_speed_ms": values.get(field) if field else None}


class TelemetrySource(Protocol):
    def __aiter__(self) -> AsyncIterator[Snapshot]: ...
    async def aclose(self) -> None: ...


class KrpcTelemetry:
    def __init__(self, settings: dict | None = None, *, connect=None) -> None:
        self.settings = settings or {}
        self.interval_s = positive(self.settings.get("poll_interval_s", 0.5), "poll_interval_s")
        self.timeout = positive(self.settings.get("timeout_s", 3.0), "timeout_s")
        self.connect = connect or krpc.connect
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="familiar-ksp")
        self.connection = None
        self.closed = False
        self.last_error = ""
        self.resources = load_resources()
        self._propellant_cache: dict[tuple[str, int], tuple[str, ...]] = {}

    def _close(self) -> None:
        connection, self.connection = self.connection, None
        if connection is None:
            return
        with suppress(Exception):
            connection.close()

    def _poll(self) -> Snapshot:
        try:
            if self.connection is None:
                self.connection = self.connect(
                    name="Familiar",
                    address=self.settings.get("address", "127.0.0.1"),
                    rpc_port=int(self.settings.get("rpc_port", 50000)),
                    stream_port=None,
                    timeout=self.timeout,
                )
            snapshot = read_snapshot(
                self.connection,
                cache=self._propellant_cache,
                resources_config=self.resources,
            )
            self.last_error = ""
            return snapshot
        except Exception as exc:
            self.last_error = str(exc)
            self._close()
            return Snapshot()

    async def poll(self) -> Snapshot:
        if self.closed:
            return Snapshot()
        return await asyncio.get_running_loop().run_in_executor(self.worker, self._poll)

    async def __aiter__(self) -> AsyncIterator[Snapshot]:
        delay = self.interval_s
        try:
            while not self.closed:
                snapshot = await self.poll()
                yield snapshot
                if snapshot.connected:
                    delay = self.interval_s
                    await asyncio.sleep(delay)
                    continue
                await asyncio.sleep(random.uniform(0.8, 1.0) * min(delay, 10.0))
                delay = min(delay * 2, 10.0)
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await asyncio.get_running_loop().run_in_executor(self.worker, self._close)
        finally:
            self.worker.shutdown(wait=False)


class ReplayTelemetry:
    """JSONL snapshot dictionaries, optionally wrapped in {ts, snapshot}."""

    def __init__(self, path: str | Path, speed: float = 1.0) -> None:
        self.path = Path(path)
        self.speed = positive(speed, "replay_speed")
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[Snapshot]:
        previous = None
        with self.path.open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                if self.closed:
                    return
                try:
                    item = json.loads(line)
                    timestamp = float(item.get("ts", 0 if previous is None else previous + 0.5))
                    if not math.isfinite(timestamp) or (
                        previous is not None and timestamp < previous
                    ):
                        raise ValueError("timestamps must be finite and nondecreasing")
                    snapshot = Snapshot.from_dict(item.get("snapshot", item))
                except (TypeError, ValueError, AttributeError) as exc:
                    raise ConfigError(f"{self.path}:{number}: invalid replay: {exc}") from exc
                if previous is not None:
                    await asyncio.sleep((timestamp - previous) / self.speed)
                previous = timestamp
                yield snapshot

    async def aclose(self) -> None:
        self.closed = True
