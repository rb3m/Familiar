"""Stateful edge detection composed from small rule functions."""

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial

from familiar.interfaces import GameEvent

from .geo import haversine
from .knowledge import KnowledgePack
from .places import STATE_NAMES, Place, PlaceIndex
from .telemetry import CARGO_DAMAGE_TO_PERCENT, Snapshot, number
from .units import Units, duration_text

# (snapshot flag, event kind, priority, details field)
EDGE_RULES = (
    ("fuel_warning", "fuel_low", "critical", None),
    ("fined", "fined", "critical", "fined_details"),
    ("job_cancelled", "job_cancelled", "critical", "cancelled_details"),
    ("on_job", "job_started", "notable", None),
    ("job_delivered", "job_delivered", "notable", "delivered_details"),
    ("ferry", "ferry", "notable", "ferry_details"),
    ("train", "train", "notable", "train_details"),
    ("refuel_paid", "refuel_paid", "notable", None),
)
WARNING_RULES = {
    "air_pressure": ("air_pressure_warning", "air_pressure_emergency"),
    "engine_warning": (
        "oil_pressure_warning",
        "water_temperature_warning",
        "battery_voltage_warning",
    ),
}


def _fuel_summary(s: Snapshot, d: dict, u: Units) -> tuple[str, dict]:
    return f"Fuel low: {s.fuel_fraction:.0%}, {u.volume(s.fuel)} remaining.", {
        "fuel_litres": s.fuel,
        "fuel_fraction": s.fuel_fraction,
    }


def _fine_summary(s: Snapshot, d: dict, u: Units) -> tuple[str, dict]:
    amount = number(d.get("amount"))
    offence = str(d.get("offence", "unknown offence"))
    return f"Fined {u.money(amount)} for {offence}.", {"offence": offence, "amount": amount}


def _cancel_summary(s: Snapshot, d: dict, u: Units) -> tuple[str, dict]:
    penalty = number(d.get("penalty"))
    return f"Job cancelled; penalty {u.money(penalty)}.", {"penalty": penalty}


def _job_summary(s: Snapshot, d: dict, u: Units) -> tuple[str, dict]:
    data = {
        "cargo": s.cargo,
        "source_city": s.source_city,
        "source_company": s.source_company,
        "destination_city": s.destination_city,
        "destination_company": s.destination_company,
        "planned_distance_km": s.planned_distance_km,
        "deadline_remaining_s": _seconds(s.job_remaining),
    }
    summary = (
        f"Job started: {s.cargo} from {s.source_city} ({s.source_company}) "
        f"to {s.destination_city} ({s.destination_company}), "
        f"{u.distance(s.planned_distance_km)}, deadline in "
        f"{duration_text(s.job_remaining)} (game time)."
    )
    return summary, data


def _delivery_summary(s: Snapshot, d: dict, u: Units) -> tuple[str, dict]:
    data = {
        "revenue": number(d.get("revenue")),
        "xp": number(d.get("earnedXp")),
        "damage_percent": number(d.get("cargoDamage")) * CARGO_DAMAGE_TO_PERCENT,
        "distance_km": number(d.get("distanceKm")),
    }
    summary = (
        f"Job delivered: {u.money(data['revenue'])}, {data['xp']:g} XP, "
        f"cargo damage {data['damage_percent']:g}%, "
        f"{u.distance(data['distance_km'])} driven."
    )
    return summary, data


def _transport_summary(kind: str, s: Snapshot, d: dict, u: Units) -> tuple[str, dict]:
    data = {
        "source_name": str(d.get("sourceName", "")),
        "target_name": str(d.get("targetName", "")),
        "price": number(d.get("payAmount")),
    }
    summary = (
        f"Taking the {kind} from {data['source_name']} to "
        f"{data['target_name']}, fare {u.money(data['price'])}."
    )
    return summary, data


def _refuel_summary(s: Snapshot, d: dict, u: Units) -> tuple[str, dict]:
    return f"Refuelling paid; fuel now {u.volume(s.fuel)} ({s.fuel_fraction:.0%}).", {
        "fuel_litres": s.fuel,
    }


SUMMARY_BUILDERS = {
    "fuel_low": _fuel_summary,
    "fined": _fine_summary,
    "job_cancelled": _cancel_summary,
    "job_started": _job_summary,
    "job_delivered": _delivery_summary,
    "ferry": partial(_transport_summary, "ferry"),
    "train": partial(_transport_summary, "train"),
    "refuel_paid": _refuel_summary,
}


class EventDetector:
    def __init__(
        self,
        projection: Callable[[float, float], tuple[float, float]],
        places: PlaceIndex,
        knowledge: KnowledgePack,
        units: Units,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.projection = projection
        self.places = places
        self.knowledge = knowledge
        self.units = units
        self.clock = clock
        self.previous = Snapshot()
        self.state: str | None = None
        self._candidate: str | None = None
        self._state_count = 0
        self._landmarks: set[str] = set()
        self._towns: set[tuple[str, str]] = set()
        self._rest_warned = False
        self._deadline_warned = False
        self._speed_since: float | None = None
        self._last_speed_event = float("-inf")

    def _event(self, kind: str, priority: str, summary: str, **data) -> GameEvent:
        return GameEvent(kind, priority, {**data, "summary": summary}, datetime.now(UTC))

    def feed(self, snapshot: Snapshot) -> list[GameEvent]:
        events = self._transitions(snapshot)
        if not snapshot.connected:
            self._speed_since = None
            self._candidate = None
            self._state_count = 0
            self.previous = snapshot
            return events
        for rule in (self._edges, self._warnings, self._rest, self._deadline, self._speeding):
            events.extend(rule(snapshot))
        if snapshot.paused:
            self._candidate, self._state_count = None, 0
            self.previous = snapshot
            return events
        lat, lon = self.projection(snapshot.x, snapshot.z)
        place = self.places.nearest(lat, lon)
        events.extend(self._state(place))
        events.extend(self._landmark_events(lat, lon))
        events.extend(self._town(place))
        self.previous = snapshot
        return events

    def _transitions(self, s: Snapshot) -> list[GameEvent]:
        events = []
        for field, labels in (
            ("connected", {True: "connected", False: "disconnected"}),
            ("paused", {True: "paused", False: "unpaused"}),
        ):
            if getattr(s, field) == getattr(self.previous, field):
                continue
            # A disconnected payload cannot reliably report pause transitions.
            if field == "paused" and not s.connected:
                continue
            state = labels[getattr(s, field)]
            events.append(
                self._event("game_state", "ambient", f"Game {state}.", state=state, ttl_s=0)
            )
        return events

    def _edges(self, s: Snapshot) -> list[GameEvent]:
        events = []
        for flag, kind, priority, details_field in EDGE_RULES:
            if not getattr(s, flag) or getattr(self.previous, flag):
                continue
            details = dict(getattr(s, details_field)) if details_field else {}
            summary, details = SUMMARY_BUILDERS[kind](s, details, self.units)
            events.append(self._event(kind, priority, summary, **details))
        return events

    def _warnings(self, s: Snapshot) -> list[GameEvent]:
        events = []
        for kind, flags in WARNING_RULES.items():
            rising = [
                flag for flag in flags if getattr(s, flag) and not getattr(self.previous, flag)
            ]
            if not rising:
                continue
            names = ", ".join(flag.replace("_", " ") for flag in rising)
            events.append(
                self._event(kind, "critical", f"Truck warning: {names}.", warnings=rising)
            )
        return events

    def _state(self, place: Place | None) -> list[GameEvent]:
        if place is None:
            self._candidate, self._state_count = None, 0
            return []
        if place.state != self._candidate:
            self._candidate, self._state_count = place.state, 0
        self._state_count += 1
        if self._state_count < 3 or place.state == self.state:
            return []
        previous, self.state = self.state, place.state
        if previous is None:
            return []
        origin = STATE_NAMES.get(previous, previous)
        target = STATE_NAMES.get(place.state, place.state)
        return [
            self._event(
                "entered_state",
                "notable",
                f"Entered {target} from {origin}.",
                from_state=origin,
                to_state=target,
                from_code=previous,
                to_code=place.state,
            )
        ]

    def _landmark_events(self, lat: float, lon: float) -> list[GameEvent]:
        self._landmarks.intersection_update(
            p.id
            for p in self.knowledge.landmarks
            if haversine(lat, lon, p.lat, p.lon) <= p.radius_km * 1.5
        )
        events = []
        for landmark, distance in self.knowledge.nearby(lat, lon):
            if landmark.id in self._landmarks:
                continue
            self._landmarks.add(landmark.id)
            events.append(
                self._event(
                    "landmark_nearby",
                    "notable",
                    # No distance in the summary: positions come from the compressed game
                    # map (~25 km off), and the companion repeats numbers as exact.
                    f"We are in the area of {landmark.name}.",
                    id=landmark.id,
                    name=landmark.name,
                    state=landmark.state,
                    landmark_kind=landmark.kind,
                    highway=landmark.highway,
                    distance_km=distance,
                    facts=list(landmark.facts),
                    source=landmark.source,
                    ttl_s=120,
                )
            )
        return events

    def _town(self, place: Place | None) -> list[GameEvent]:
        if place is None or place.distance_km > 3 or place.land_km2 < 20:
            return []
        key = (place.name, place.state)
        if key in self._towns:
            return []
        self._towns.add(key)
        state = STATE_NAMES.get(place.state, place.state)
        return [
            self._event(
                "passing_town",
                "ambient",
                f"Passing {place.name}, {state}.",
                name=place.name,
                state=state,
                distance_km=place.distance_km,
            )
        ]

    def _rest(self, s: Snapshot) -> list[GameEvent]:
        remaining = s.rest_remaining
        if remaining is None:
            return []
        if remaining >= timedelta(hours=1):
            self._rest_warned = False
            return []
        if self._rest_warned:
            return []
        self._rest_warned = True
        return [
            self._event(
                "rest_soon",
                "notable",
                f"Rest needed in {duration_text(remaining)} (game time).",
                remaining_s=remaining.total_seconds(),
            )
        ]

    def _deadline(self, s: Snapshot) -> list[GameEvent]:
        if not s.on_job or not self.previous.on_job:
            self._deadline_warned = False
        if (
            not s.on_job
            or self._deadline_warned
            or s.job_remaining is None
            or s.nav_estimated is None
        ):
            return []
        if s.job_remaining >= s.nav_estimated * 1.1:
            return []
        self._deadline_warned = True
        return [
            self._event(
                "deadline_tight",
                "notable",
                f"Deadline tight: {duration_text(s.job_remaining)} left, ETA "
                f"{duration_text(s.nav_estimated)} (game time).",
                remaining_s=s.job_remaining.total_seconds(),
                estimated_s=s.nav_estimated.total_seconds(),
            )
        ]

    def _speeding(self, s: Snapshot) -> list[GameEvent]:
        now = self.clock()
        if not s.driving or s.speed_limit <= 0 or s.speed <= s.speed_limit + 16:
            self._speed_since = None
            return []
        if self._speed_since is None:
            self._speed_since = now
        if now - self._speed_since < 20 or now - self._last_speed_event < 600:
            return []
        self._last_speed_event = now
        return [
            self._event(
                "speeding",
                "ambient",
                f"Speeding: {self.units.speed(s.speed)} in a "
                f"{self.units.speed(s.speed_limit)} zone.",
                speed_kmh=s.speed,
                limit_kmh=s.speed_limit,
            )
        ]


def _seconds(value: timedelta | None) -> float | None:
    if value is None:
        return None
    return value.total_seconds()
