"""ATS and ETS2 adapters sharing telemetry and event detection."""

import asyncio
import math
from collections.abc import AsyncIterator
from dataclasses import asdict
from time import monotonic

from familiar.interfaces import (
    PLUGIN_API_VERSION,
    ActionSpec,
    ConfigError,
    GameEvent,
    Keyboard,
    ToolSpec,
    WatchSample,
    WatchSpec,
)

from .actions import action_arguments, action_mixes, action_specs, load_catalogue
from .bindings import GAME_NAMES, BindingSource
from .events import EventDetector
from .geo import compass_name, day_period, sun_elevation, true_heading
from .knowledge import KnowledgePack
from .places import Place, PlaceIndex, _packaged_places, load_regions
from .projection import projection_for_game
from .telemetry import (
    DEFAULT_URL,
    HttpTelemetry,
    ReplayTelemetry,
    Snapshot,
    TelemetrySource,
    _game_clock,
    game_hour,
)
from .units import Units, duration_text, rest_text

READBACK_TIMEOUT_S = 2.5
READBACK_STEP_S = 0.25


class ScsAdapter:
    game = "ats"
    name = "scs"
    api_version = PLUGIN_API_VERSION
    capabilities = frozenset({"events", "snapshot"})

    def __init__(self, settings: dict) -> None:
        default_units = {"ats": "imperial", "ets2": "metric"}[self.game]
        self.units = Units(settings.get("units", default_units), self.game)
        self.projection = projection_for_game(self.game)
        self.region_names = load_regions()[self.game]
        self.places = self._place_index()
        self.knowledge = KnowledgePack.for_game(self.game, settings.get("knowledge_paths"))
        self.detector = EventDetector(
            self.projection, self.places, self.knowledge, self.units, region_names=self.region_names
        )
        self._catalogue = load_catalogue()
        self._mixes = action_mixes(self._catalogue)
        self._binding_source = BindingSource(self.game, settings, self._mixes)
        self.bindings = self._binding_source.table
        self._keyboard: Keyboard | None = None
        self._hold_s = {name: entry.get("hold_s", 0.08) for name, entry in self._catalogue.items()}
        for action, value in settings.get("hold_s", {}).items():
            if action not in self._mixes:
                raise ConfigError(f"Unknown SCS hold_s action: {action}")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"hold_s.{action} must be positive and finite")
            if not math.isfinite(value) or value <= 0:
                raise ConfigError(f"hold_s.{action} must be positive and finite")
            self._hold_s[action] = value
        self.latest = Snapshot()
        self._watch_timestamp = monotonic()
        self.source: TelemetrySource
        if settings.get("replay"):
            self.source = ReplayTelemetry(settings["replay"], settings.get("replay_speed", 1.0))
            return
        self.source = HttpTelemetry(
            settings.get("url", DEFAULT_URL), settings.get("poll_interval_s", 1.0)
        )

    def attach_keyboard(self, keyboard: Keyboard) -> None:
        self._keyboard = keyboard

    def actions(self) -> dict[str, ActionSpec]:
        return action_specs(self._catalogue)

    def action_context(self) -> str:
        s = self.latest
        identity = [GAME_NAMES[self.game]]
        if s.make or s.model:
            identity.extend((s.make, s.model))
        if s.on_job:
            identity.extend(
                (
                    s.source_city,
                    s.source_company,
                    s.destination_city,
                    s.destination_company,
                    s.cargo,
                    s.deadline_time,
                )
            )
        return repr(identity) if len(identity) > 1 else identity[0]

    def _action_guard(self, name: str, mix: str) -> str | None:
        self.bindings = self._binding_source.refresh()
        if self._keyboard is None:
            return "No keyboard attached"
        if not self.latest.connected:
            return "Game telemetry is disconnected"
        if monotonic() - self._watch_timestamp >= 2:
            return "Game telemetry is stale (sample age must be less than 2 seconds)"
        if self.latest.paused:
            return "Game is paused or in menus"
        if mix not in self.bindings:
            return f"No keyboard binding for {name} in your profile; it may be joystick-only"
        return None

    async def _await_state(self, field: str | None, desired: bool) -> None:
        """Wait for telemetry (polled about once a second) to show the result of a press."""
        deadline = monotonic() + READBACK_TIMEOUT_S
        await asyncio.sleep(READBACK_STEP_S)
        while monotonic() < deadline:
            if field and getattr(self.latest, field) == desired:
                return
            await asyncio.sleep(READBACK_STEP_S)

    async def perform(self, name: str, arguments: dict) -> dict:
        if name not in self._mixes:
            return {"status": "error", "detail": f"Unknown SCS action: {name}"}
        expected = action_arguments(self._catalogue[name])
        if expected:
            argument, choices = expected
            if set(arguments) != {argument} or arguments[argument] not in choices:
                return {"status": "error", "detail": f"{argument} must be one of {choices}"}
        if not expected and arguments:
            return {"status": "error", "detail": f"{name} takes no arguments"}
        mix = self._mixes[name]
        if isinstance(mix, dict):
            mix = mix[arguments[expected[0]]]
        reason = self._action_guard(name, mix)
        if reason:
            return {"status": "unavailable", "detail": reason}
        field = self._catalogue[name].get("state_field")
        desired = bool(expected and arguments.get(expected[0]) == "on")
        if field and expected and getattr(self.latest, field) == desired:
            return {"status": "already", "detail": f"{name} is already {arguments[expected[0]]}"}
        await self._keyboard.tap(self.bindings[mix], hold_s=self._hold_s.get(name, 0.08))
        if field:
            await self._await_state(field if expected else None, desired)
            reason = self._action_guard(name, mix)
            if reason:
                return {"status": "unknown", "detail": f"Key pressed; {reason}"}
            if not expected:
                return {
                    "status": "done",
                    "detail": f"{name.capitalize()} cycled once",
                    field: getattr(self.latest, field),
                }
            if getattr(self.latest, field) == desired:
                return {"status": "done", "detail": f"{name} is now {arguments[expected[0]]}"}
            return {"status": "unknown", "detail": f"Key pressed; {name} state did not change"}
        detail = (
            "toggled; state is not observable"
            if self._catalogue[name].get("toggle", False)
            else "key pressed"
        )
        return {"status": "done", "detail": f"{name}: {detail}"}

    def _place_index(self) -> PlaceIndex:
        filename = {"ats": "us_places.csv", "ets2": "eu_places.csv"}[self.game]
        return PlaceIndex(places=_packaged_places(filename))

    async def events(self) -> AsyncIterator[GameEvent]:
        try:
            async for snapshot in self.source:
                self.latest = snapshot
                self._watch_timestamp = monotonic()
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

    def whereabouts(self) -> dict | None:
        s = self.latest
        if not s.connected:
            return None
        _, _, place = self._location()
        if place is None:
            return None
        activity = "driving without a job"
        if s.on_job:
            activity = (
                f"hauling {self.units.mass(s.cargo_mass)} of {s.cargo} to {s.destination_city}"
            )
        return {
            "place_key": f"{place.state}/{place.name}",
            "place": place.name,
            "region": self.region_names.get(place.state, place.state),
            "activity": activity,
        }

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
        hour = game_hour(s.game_time)
        if hour is not None:
            period = day_period(sun_elevation(lat, hour), hour)
            clock_text = _game_clock(s.game_time).strftime("%H:%M")
            lines.append(f"Time: {clock_text} in-game, {period}")
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

    def watchables(self) -> dict[str, WatchSpec]:
        return {
            "route_distance": WatchSpec(
                "m", "remaining navigation route distance, not straight-line distance"
            ),
            "speed": WatchSpec("m/s", "truck speed"),
            "fuel": WatchSpec("%", "fuel remaining as a percentage of tank capacity"),
            "fuel_range": WatchSpec("m", "estimated remaining range on current fuel"),
            "deadline_remaining": WatchSpec("s", "job deadline remaining in game time"),
            "rest_remaining": WatchSpec("s", "time until required rest in game time"),
            "speed_over_limit": WatchSpec(
                "m/s", "speed minus the known posted limit, positive when speeding"
            ),
        }

    def watch_sample(self) -> WatchSample:
        s = self.latest
        context = "free roam"
        if s.on_job:
            context = (
                f"{s.source_city} ({s.source_company})->"
                f"{s.destination_city} ({s.destination_company})"
            )
        values = {
            "route_distance": s.nav_distance_km * 1000
            if s.nav_distance_km > 0 or s.nav_estimated is not None or s.on_job
            else None,
            "speed": s.speed / 3.6,
            "fuel": 100 * s.fuel_fraction if s.fuel_capacity > 0 else None,
            "fuel_range": s.fuel_range * 1000 if s.fuel_capacity > 0 else None,
            "deadline_remaining": s.job_remaining.total_seconds()
            if s.on_job and s.job_remaining is not None
            else None,
            "rest_remaining": s.rest_remaining.total_seconds()
            if s.rest_remaining is not None
            else None,
            "speed_over_limit": (s.speed - s.speed_limit) / 3.6 if s.speed_limit > 0 else None,
        }
        return WatchSample(
            values, context, s.connected and not s.paused, True, self._watch_timestamp
        )

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
