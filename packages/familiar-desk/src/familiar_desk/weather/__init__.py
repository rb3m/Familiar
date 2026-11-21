"""Pluggable weather providers and the normalised report they return (SI units)."""

import math
from dataclasses import dataclass, field
from importlib.metadata import entry_points, version
from typing import Protocol

import httpx
from familiar.interfaces import ConfigError, load_data

PROVIDER_GROUP = "familiar_desk.weather_providers"


@dataclass(frozen=True)
class Place:
    latitude: float | None = None
    longitude: float | None = None
    timezone: str | None = None
    location: str | None = None
    station: str | None = None


@dataclass(frozen=True)
class Current:
    observed_at: str
    condition: str = "unknown"
    temperature_c: float | None = None
    feels_like_c: float | None = None
    humidity_pct: float | None = None
    cloud_cover_pct: float | None = None
    precipitation_mm: float | None = None  # over the provider's latest interval
    wind_m_s: float | None = None


@dataclass(frozen=True)
class Window:
    name: str
    condition: str
    min_c: float | None
    max_c: float | None
    rain_chance_pct: float | None


@dataclass(frozen=True)
class Today:
    date: str
    min_c: float | None = None
    max_c: float | None = None
    rain_chance_pct: float | None = None
    precipitation_mm: float | None = None
    condition: str = "unknown"
    windows: tuple[Window, ...] = ()


@dataclass(frozen=True)
class WeatherReport:
    provider: str
    current: Current | None = None
    today: Today | None = None
    sources: tuple[str, ...] = field(default_factory=tuple)


class WeatherProvider(Protocol):
    """Fetch what the provider knows; leave the rest None."""

    name: str

    def __init__(self, settings: dict) -> None: ...

    async def fetch(self, place: Place, client: httpx.AsyncClient) -> WeatherReport: ...


def weather_settings(overrides: dict | None) -> dict:
    settings = {**load_data("familiar_desk", "weather").value, **(overrides or {})}
    for key, minimum in (("interval_s", 60), ("max_station_km", 0), ("max_age_s", 60)):
        _number(settings, key, minimum)
    _number(settings, "timeout_s", 0, positive=True)
    _number(settings, "remark_every_h", 0)
    _number(settings, "outlook_hour", 0, maximum=23, integer=True)
    hours = settings["active_hours"]
    valid_hours = (
        isinstance(hours, list)
        and len(hours) == 2
        and all(type(hour) is int and 0 <= hour <= 24 for hour in hours)
        and hours[0] < hours[1]
    )
    if not valid_hours:
        raise ConfigError("desk.weather.active_hours must be [start, end] hours, start < end")
    providers = settings["providers"]
    if not isinstance(providers, list) or not all(isinstance(p, str) and p for p in providers):
        raise ConfigError("desk.weather.providers must be a list of provider names")
    station = settings["station"]
    if station is not None and (not isinstance(station, str) or not station.strip()):
        raise ConfigError("desk.weather.station must be an ICAO code such as MMQT")
    return settings


def _number(settings, key, minimum, *, maximum=math.inf, positive=False, integer=False):
    value = settings[key]
    valid = (
        type(value) in {int, float}
        and math.isfinite(value)
        and minimum <= value <= maximum
        and not (positive and value == 0)
        and not (integer and type(value) is not int)
    )
    if not valid:
        raise ConfigError(f"desk.weather.{key} is outside its allowed range")


def load_providers(settings: dict) -> list[WeatherProvider]:
    found = {entry.name: entry for entry in entry_points(group=PROVIDER_GROUP)}
    missing = [name for name in settings["providers"] if name not in found]
    if missing:
        names = ", ".join(sorted(found)) or "(none)"
        raise ConfigError(f"Unknown weather provider {missing[0]!r}. Available: {names}")
    return [found[name].load()(settings) for name in dict.fromkeys(settings["providers"])]


def user_agent() -> str:
    return f"Familiar/{version('familiar-desk')} (desk weather)"


def worst(conditions, severity: list[str]) -> str:
    ranked = [c for c in conditions if c in severity]
    return max(ranked, key=severity.index, default="unknown")
