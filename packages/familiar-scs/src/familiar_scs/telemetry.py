"""Funbit parsing assumptions, polling, replay and recording."""

import asyncio
import json
import math
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

import httpx
from familiar.interfaces import ConfigError

DEFAULT_URL = "http://127.0.0.1:31377/api/ets2/telemetry"
# Funbit converts speed to km/h; distance is metres, fuel litres, range km.
SPEED_TO_KMH = 1.0
NAV_DISTANCE_TO_KM = 0.001
FUEL_TO_LITRES = 1.0
FUEL_RANGE_TO_KM = 1.0
# Funbit cargo damage is assumed to be a fraction (0..1).
CARGO_DAMAGE_TO_PERCENT = 100.0
# Durations use an ISO datetime offset from year 0001, not wall/game clock time.
# Exception: game.nextRestStopTime is an absolute game-clock time (server: GameTime + NextRestStop),
# so the time left is nextRestStopTime - game.time (see time_until).
DURATION_EPOCH = datetime(1, 1, 1)


def number(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (ValueError, TypeError, OverflowError):
        return default


def _boolean(value: object) -> bool:
    return value is True


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _game_clock(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None)
    except ValueError:
        return None


def time_until(target: object, now: object) -> timedelta | None:
    """Game time left until an absolute game-clock ``target``; zero once it has passed."""
    target_time, now_time = _game_clock(target), _game_clock(now)
    if target_time is None or now_time is None or target_time <= DURATION_EPOCH:
        return None
    return max(target_time - now_time, timedelta(0))


def parse_duration(value: object) -> timedelta | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value).replace(tzinfo=None)
        duration = parsed - DURATION_EPOCH
        if duration <= timedelta(0):
            return None
        return duration
    except ValueError:
        return None


@dataclass(frozen=True)
class TrailerDamage:
    # The supplied trailer schema has no ID; list slots identify the attached set.
    slots: tuple[int, ...] = ()
    chassis: float = 0.0
    body: float = 0.0
    wheels: float = 0.0
    cargo: float = 0.0


def _trailer_damage(value: object) -> TrailerDamage:
    if not isinstance(value, list):
        return TrailerDamage()
    attached = [(i, _mapping(item)) for i, item in enumerate(value)]
    attached = [(i, item) for i, item in attached if _boolean(item.get("attached"))]
    totals = {
        name: sum(max(0.0, min(1.0, number(item.get(key)))) for _, item in attached)
        for name, key in {
            "chassis": "wearChassis",
            "body": "wearBody",
            "wheels": "wearWheels",
            "cargo": "cargoDamage",
        }.items()
    }
    return TrailerDamage(slots=tuple(i for i, _ in attached), **totals)


@dataclass(frozen=True)
class Snapshot:
    connected: bool = False
    paused: bool = False
    game_name: str = ""
    game_time: str = ""
    time_scale: float = 0.0
    speed: float = 0.0
    speed_limit: float = 0.0
    fuel: float = 0.0
    fuel_capacity: float = 0.0
    fuel_average_consumption: float = 0.0
    fuel_range: float = 0.0
    adblue: float = 0.0
    engine_on: bool = False
    park_brake_on: bool = False
    cruise_control_on: bool = False
    fuel_warning: bool = False
    air_pressure_warning: bool = False
    air_pressure_emergency: bool = False
    oil_pressure_warning: bool = False
    water_temperature_warning: bool = False
    battery_voltage_warning: bool = False
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    heading: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    make: str = ""
    model: str = ""
    odometer: float = 0.0
    income: float = 0.0
    deadline_time: str = ""
    planned_distance_km: float = 0.0
    source_city: str = ""
    source_company: str = ""
    destination_city: str = ""
    destination_company: str = ""
    cargo: str = ""
    cargo_mass: float = 0.0
    job_market: str = ""
    nav_distance_km: float = 0.0
    job_remaining: timedelta | None = None
    nav_estimated: timedelta | None = None
    rest_remaining: timedelta | None = None
    on_job: bool = False
    job_finished: bool = False
    job_cancelled: bool = False
    job_delivered: bool = False
    fined: bool = False
    tollgate: bool = False
    ferry: bool = False
    train: bool = False
    refuel: bool = False
    refuel_paid: bool = False
    delivered_details: dict = field(default_factory=dict)
    cancelled_details: dict = field(default_factory=dict)
    fined_details: dict = field(default_factory=dict)
    tollgate_details: dict = field(default_factory=dict)
    ferry_details: dict = field(default_factory=dict)
    train_details: dict = field(default_factory=dict)
    trailer_damage: TrailerDamage = field(default_factory=TrailerDamage)

    @property
    def fuel_fraction(self) -> float:
        if self.fuel_capacity <= 0:
            return 0.0
        return max(0.0, min(1.0, self.fuel / self.fuel_capacity))

    @property
    def driving(self) -> bool:
        return self.connected and not self.paused and self.speed > 3

    @classmethod
    def from_json(cls, payload: object) -> "Snapshot":
        root = _mapping(payload)
        game = _mapping(root.get("game"))
        if not _boolean(game.get("connected")):
            return cls()
        sections = {
            name: _mapping(root.get(name))
            for name in ("game", "truck", "job", "navigation", "gameplay")
        }
        sections["placement"] = _mapping(sections["truck"].get("placement"))
        values = {
            name: parser(sections[section].get(key))
            for name, (section, key, parser) in FIELD_PARSERS.items()
        }
        values["rest_remaining"] = time_until(game.get("nextRestStopTime"), game.get("time"))
        values["trailer_damage"] = _trailer_damage(root.get("trailers"))
        return cls(**values)


# Every schema/unit assumption is confined to this module.
FIELD_PARSERS = {
    "connected": ("game", "connected", _boolean),
    "paused": ("game", "paused", _boolean),
    "game_name": ("game", "gameName", _text),
    "game_time": ("game", "time", _text),
    "time_scale": ("game", "timeScale", number),
    "speed": ("truck", "speed", lambda v: number(v) * SPEED_TO_KMH),
    "speed_limit": ("navigation", "speedLimit", lambda v: number(v) * SPEED_TO_KMH),
    "fuel": ("truck", "fuel", lambda v: number(v) * FUEL_TO_LITRES),
    "fuel_range": ("truck", "fuelRange", lambda v: number(v) * FUEL_RANGE_TO_KM),
    "nav_distance_km": (
        "navigation",
        "estimatedDistance",
        lambda v: number(v) * NAV_DISTANCE_TO_KM,
    ),
    "job_remaining": ("job", "remainingTime", parse_duration),
    "nav_estimated": ("navigation", "estimatedTime", parse_duration),
}
for _section, _parser, _fields in (
    (
        "truck",
        number,
        {
            "fuel_capacity": "fuelCapacity",
            "fuel_average_consumption": "fuelAverageConsumption",
            "adblue": "adblue",
            "odometer": "odometer",
        },
    ),
    (
        "truck",
        _boolean,
        {
            "engine_on": "engineOn",
            "park_brake_on": "parkBrakeOn",
            "cruise_control_on": "cruiseControlOn",
            "fuel_warning": "fuelWarningOn",
            "air_pressure_warning": "airPressureWarningOn",
            "air_pressure_emergency": "airPressureEmergencyOn",
            "oil_pressure_warning": "oilPressureWarningOn",
            "water_temperature_warning": "waterTemperatureWarningOn",
            "battery_voltage_warning": "batteryVoltageWarningOn",
        },
    ),
    ("truck", _text, {"make": "make", "model": "model"}),
    ("placement", number, {name: name for name in ("x", "y", "z", "heading", "pitch", "roll")}),
    (
        "job",
        number,
        {"income": "income", "planned_distance_km": "plannedDistanceKm", "cargo_mass": "cargoMass"},
    ),
    (
        "job",
        _text,
        {
            "deadline_time": "deadlineTime",
            "source_city": "sourceCity",
            "source_company": "sourceCompany",
            "destination_city": "destinationCity",
            "destination_company": "destinationCompany",
            "cargo": "cargo",
            "job_market": "jobMarket",
        },
    ),
    (
        "gameplay",
        _boolean,
        {
            "on_job": "onJob",
            "job_finished": "jobFinished",
            "job_cancelled": "jobCancelled",
            "job_delivered": "jobDelivered",
            "fined": "fined",
            "tollgate": "tollgate",
            "ferry": "ferry",
            "train": "train",
            "refuel": "refuel",
            "refuel_paid": "refuelPayed",
        },
    ),
    (
        "gameplay",
        _mapping,
        {
            "delivered_details": "jobDeliveredDetails",
            "cancelled_details": "jobCancelledDetails",
            "fined_details": "finedDetails",
            "tollgate_details": "tollgateDetails",
            "ferry_details": "ferryDetails",
            "train_details": "trainDetails",
        },
    ),
):
    FIELD_PARSERS.update({name: (_section, key, _parser) for name, key in _fields.items()})


class TelemetrySource(Protocol):
    def __aiter__(self) -> AsyncIterator[Snapshot]: ...
    async def aclose(self) -> None: ...


def positive(value: float, name: str) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be positive and finite")
    return value


class HttpTelemetry:
    def __init__(
        self,
        url: str = DEFAULT_URL,
        interval_s: float = 1.0,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url
        self.interval_s = positive(interval_s, "poll_interval_s")
        self.client = httpx.AsyncClient(timeout=5.0, transport=transport)

    async def payloads(self) -> AsyncIterator[dict]:
        delay = self.interval_s
        while True:
            try:
                response = await self.client.get(self.url)
                response.raise_for_status()
                payload = _mapping(response.json())
            except (httpx.HTTPError, ValueError):
                yield {}
                await asyncio.sleep(min(delay, 10.0))
                delay = min(delay * 2, 10.0)
                continue
            delay = self.interval_s
            yield payload
            await asyncio.sleep(self.interval_s)

    async def __aiter__(self) -> AsyncIterator[Snapshot]:
        async for payload in self.payloads():
            yield Snapshot.from_json(payload)

    async def aclose(self) -> None:
        await self.client.aclose()


class ReplayTelemetry:
    def __init__(self, path: str | Path, speed: float = 1.0) -> None:
        self.path = Path(path)
        self.speed = positive(speed, "replay_speed")

    async def __aiter__(self) -> AsyncIterator[Snapshot]:
        previous: float | None = None
        with self.path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                try:
                    item = json.loads(line)
                    ts = float(item["ts"])
                    payload = item["telemetry"]
                    if not math.isfinite(ts) or not isinstance(payload, dict):
                        raise ValueError("invalid timestamp or telemetry")
                    if previous is not None and ts < previous:
                        raise ValueError("timestamps must be nondecreasing")
                except (TypeError, ValueError, KeyError, OverflowError) as exc:
                    raise ConfigError(f"{self.path}:{line_number}: invalid replay: {exc}") from exc
                if previous is not None:
                    await asyncio.sleep((ts - previous) / self.speed)
                previous = ts
                yield Snapshot.from_json(payload)

    async def aclose(self) -> None:
        return None


async def record(url: str, path: str | Path, interval_s: float = 1.0) -> None:
    source = HttpTelemetry(url, interval_s)
    try:
        with Path(path).open("w", encoding="utf-8") as stream:
            async for payload in source.payloads():
                stream.write(json.dumps({"ts": time.time(), "telemetry": payload}) + "\n")
                stream.flush()
    finally:
        await source.aclose()
