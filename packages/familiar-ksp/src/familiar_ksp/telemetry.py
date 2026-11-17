"""Polling and replay sources; all synchronous RPC work stays on one thread."""

import asyncio
import json
import math
import random
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Protocol

import krpc
from familiar.interfaces import ConfigError
from krpc.error import RPCError


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
    stage_liquid_fuel: float | None = None
    stage_liquid_fuel_max: float | None = None
    stage_oxidizer: float | None = None
    stage_oxidizer_max: float | None = None
    stage_solid_fuel: float | None = None
    stage_solid_fuel_max: float | None = None
    stage_monoprop: float | None = None
    stage_monoprop_max: float | None = None
    paused: bool | None = None

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
    except (AttributeError, RPCError):
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


def read_snapshot(connection) -> Snapshot:
    """Map the kRPC 0.7 object graph. Called only by the owning worker."""
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
    flight = optional(lambda: vessel.flight())  # Default is the surface reference frame.
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
    values.update(resource_values(vessel_resources, "electric_charge", "ElectricCharge"))
    values.update(resource_values(vessel_resources, "liquid_fuel", "LiquidFuel"))
    stage = values["current_stage"]
    resources = (
        optional(lambda: vessel.decouple_stage_at(stage - 1).resources(cumulative=False))
        if stage is not None
        else None
    )
    for key, name in STAGE_RESOURCES.items():
        values.update(resource_values(resources, key, name))
    return Snapshot(**values)


STAGE_RESOURCES = {
    "stage_liquid_fuel": "LiquidFuel",
    "stage_oxidizer": "Oxidizer",
    "stage_solid_fuel": "SolidFuel",
    "stage_monoprop": "MonoPropellant",
}


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
            snapshot = read_snapshot(self.connection)
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
