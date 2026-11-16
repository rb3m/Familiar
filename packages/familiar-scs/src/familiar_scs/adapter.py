"""ATS and ETS2 adapters sharing telemetry and event detection."""

from collections.abc import AsyncIterator
from dataclasses import asdict

from familiar.interfaces import PLUGIN_API_VERSION, GameEvent, ToolSpec

from .events import EventDetector
from .geo import compass_name, true_heading
from .knowledge import KnowledgePack
from .places import REGION_NAMES, Place, PlaceIndex, _packaged_places
from .projection import PROJECTIONS
from .telemetry import DEFAULT_URL, HttpTelemetry, ReplayTelemetry, Snapshot, TelemetrySource
from .units import Units, duration_text, rest_text


class ScsAdapter:
    game = "ats"
    name = "scs"
    api_version = PLUGIN_API_VERSION
    capabilities = frozenset({"events", "snapshot"})

    def __init__(self, settings: dict) -> None:
        default_units = {"ats": "imperial", "ets2": "metric"}[self.game]
        self.units = Units(settings.get("units", default_units), self.game)
        self.projection = PROJECTIONS[self.game]
        self.region_names = REGION_NAMES[self.game]
        self.places = self._place_index()
        self.knowledge = KnowledgePack.for_game(self.game, settings.get("knowledge_paths"))
        self.detector = EventDetector(
            self.projection, self.places, self.knowledge, self.units, region_names=self.region_names
        )
        self.latest = Snapshot()
        self.source: TelemetrySource
        if settings.get("replay"):
            self.source = ReplayTelemetry(settings["replay"], settings.get("replay_speed", 1.0))
            return
        self.source = HttpTelemetry(
            settings.get("url", DEFAULT_URL), settings.get("poll_interval_s", 1.0)
        )

    def _place_index(self) -> PlaceIndex:
        filename = {"ats": "us_places.csv", "ets2": "eu_places.csv"}[self.game]
        return PlaceIndex(places=_packaged_places(filename))

    async def events(self) -> AsyncIterator[GameEvent]:
        try:
            async for snapshot in self.source:
                self.latest = snapshot
                for event in self.detector.feed(snapshot):
                    yield event
        finally:
            await self.source.aclose()

    def _location(self) -> tuple[float, float, Place | None]:
        lat, lon = self.projection(self.latest.x, self.latest.z)
        return lat, lon, self.places.nearest(lat, lon)

    async def snapshot(self) -> dict:
        result = asdict(self.latest)
        result["driving"] = self.latest.driving
        for field in ("job_remaining", "nav_estimated", "rest_remaining"):
            value = result[field]
            result[field] = value.total_seconds() if value is not None else None
        result.update(lat=None, lon=None, state=None, nearest_place=None)
        if not self.latest.connected:
            return result
        lat, lon, place = self._location()
        result.update(lat=lat, lon=lon)
        if place:
            result.update(state=place.state, nearest_place=asdict(place))
        return result

    def _limit_text(self, limit_kmh: float) -> str:
        if limit_kmh <= 0:
            return "no posted speed limit known"  # telemetry reports 0 when there is none
        return f"limit {self.units.speed(limit_kmh)}"

    def describe(self) -> str:
        s = self.latest
        if not s.connected:
            return ""
        lat, lon, place = self._location()
        location = f"{lat:.4f}, {lon:.4f}"
        if place:
            state = self.region_names.get(place.state, place.state)
            location = f"near {place.name}, {state}"  # no distance: compressed game map
        lines = [
            f"Location: {location}, heading "
            f"{compass_name(true_heading(self.projection, s.x, s.z, s.heading))}",
            f"Speed: {self.units.speed(s.speed)} ({self._limit_text(s.speed_limit)})",
            f"Fuel: {s.fuel_fraction:.0%} ({self.units.volume(s.fuel)}), range "
            f"{self.units.distance(s.fuel_range)}",
        ]
        if s.on_job:
            lines.append(
                f"Job: {self.units.mass(s.cargo_mass)} of {s.cargo} from "
                f"{s.source_city} ({s.source_company}) to {s.destination_city} "
                f"({s.destination_company}), "
                f"{self.units.distance(s.nav_distance_km)} to go, ETA "
                f"{duration_text(s.nav_estimated)}, deadline in "
                f"{duration_text(s.job_remaining)} (game time)"
            )
        if s.rest_remaining is not None:
            lines.append(rest_text(s.rest_remaining))
        if s.paused:
            lines.append("Game paused")
        return "\n".join(lines)

    def tools(self) -> list[ToolSpec]:
        return []

    async def aclose(self) -> None:
        await self.source.aclose()


class AtsAdapter(ScsAdapter):
    game = "ats"
    name = "ats"
    display_name = "American Truck Simulator"
    icon = "🚛"


class Ets2Adapter(ScsAdapter):
    game = "ets2"
    name = "ets2"
    display_name = "Euro Truck Simulator 2"
    icon = "🚛"
