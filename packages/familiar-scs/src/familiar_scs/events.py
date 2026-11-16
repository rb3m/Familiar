"""Stateful edge detection composed from small rule functions."""

import math
import time
from collections import deque
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from functools import partial
from itertools import pairwise

from familiar.interfaces import GameEvent

from .geo import day_period, haversine, sun_elevation
from .knowledge import KnowledgePack
from .places import STATE_NAMES, Place, PlaceIndex
from .telemetry import CARGO_DAMAGE_TO_PERCENT, Snapshot, _game_clock, game_hour, number
from .units import Units, duration_text, rest_text

# (snapshot flag, event kind, priority, details field)
EDGE_RULES = (
    ("fuel_warning", "fuel_low", "critical", None),
    ("fined", "fined", "critical", "fined_details"),
    ("job_cancelled", "job_cancelled", "critical", "cancelled_details"),
    ("on_job", "job_started", "notable", None),
    ("job_delivered", "job_delivered", "notable", "delivered_details"),
    ("ferry", "ferry", "notable", "ferry_details"),
    ("train", "train", "notable", "train_details"),
    ("tollgate", "tollgate", "ambient", "tollgate_details"),
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
ENGINE_RUNNING_RULES = {"engine_warning"}
TOWN_MIN_POPULATION = 20_000
REST_RESET_MIN = timedelta(hours=3)
DEADLINE_MARGIN = 1.1
JAM_WINDOW_S = 120.0
JAM_LOW_SPEED_RATIO = 0.35
JAM_LOW_TIME_RATIO = 0.8
JAM_STOP_KMH = 10.0
JAM_MIN_STOPS = 2
JAM_AVERAGE_KMH = 25.0
JAM_MIN_LIMIT_KMH = 60.0
JAM_MIN_DISTANCE_KM = 1.0
JAM_RECOVERY_RATIO = 0.6
JAM_RECOVERY_S = 30.0
JAM_COOLDOWN_S = 900.0
JAM_CITY_RADIUS_KM = 25.0
JAM_CITY_POPULATION = 100_000
BRAKING_DECELERATION_MPS2 = 6.0
BRAKING_MIN_SPEED_KMH = 40.0
BRAKING_COOLDOWN_S = 60.0
BRAKING_COLLISION_WINDOW_S = 5.0
COLLISION_DAMAGE_THRESHOLD = 0.005
COLLISION_COOLDOWN_S = 30.0
DAY_PERIOD_SNAPSHOTS = 3
DAY_PERIOD_MAX_JUMP = timedelta(hours=1)
RAIN_STOP_MIN_ON_S = 60.0
RAIN_COOLDOWN_S = 300.0
DAY_PERIOD_SUMMARIES = {
    "dawn": "Dawn is breaking.",
    "day": "It's full daylight now.",
    "dusk": "Dusk is falling.",
    "night": "Night has fallen.",
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


def _toll_summary(s: Snapshot, d: dict, u: Units) -> tuple[str, dict]:
    price = number(d.get("payAmount"))
    return f"Paid a toll of {u.money(price)}.", {"price": price}


def _refuel_summary(s: Snapshot, d: dict, u: Units) -> tuple[str, dict]:
    return f"Refuelling paid; fuel now {u.volume(s.fuel)} ({s.fuel_fraction:.0%}).", {
        "fuel_litres": s.fuel,
    }


def _on_time(s: Snapshot) -> bool:
    return s.job_remaining >= s.nav_estimated * DEADLINE_MARGIN


def _slept_part(s: Snapshot, previous: Snapshot) -> tuple[str, dict]:
    before, after = _game_clock(previous.game_time), _game_clock(s.game_time)
    if before is None or after is None or after <= before:
        return "", {}
    slept = after - before
    return f" {duration_text(slept)}", {"slept_s": slept.total_seconds()}


def _deadline_part(s: Snapshot) -> tuple[str, dict]:
    if s.job_remaining is None:
        return "", {}
    text = f"; deadline in {duration_text(s.job_remaining)}"
    data = {"deadline_remaining_s": s.job_remaining.total_seconds()}
    if s.nav_estimated is None:
        return text, data
    on_time = _on_time(s)
    return text + (" (comfortable)" if on_time else " (tight)"), {**data, "on_time": on_time}


def _route_part(s: Snapshot, u: Units) -> tuple[str, dict]:
    if not s.on_job or s.nav_distance_km <= 0:
        return "", {}
    text = f" {u.distance(s.nav_distance_km)}"
    data: dict = {"nav_distance_km": s.nav_distance_km}
    if s.destination_city:
        text += f" to {s.destination_city}"
        data["destination_city"] = s.destination_city
    if s.nav_estimated is not None:
        text += f", about {duration_text(s.nav_estimated)} of driving"
        data["estimated_s"] = s.nav_estimated.total_seconds()
    deadline_text, deadline_data = _deadline_part(s)
    return text + deadline_text + ".", {**data, **deadline_data}


def _rested_summary(s: Snapshot, previous: Snapshot, u: Units) -> tuple[str, dict]:
    slept_text, slept_data = _slept_part(s, previous)
    route_text, route_data = _route_part(s, u)
    summary = (
        f"Rested{slept_text}.{route_text}"
        f" Next rest needed in {duration_text(s.rest_remaining)} (game time)."
    )
    data = {"rest_remaining_s": s.rest_remaining.total_seconds(), **slept_data, **route_data}
    return summary, data


SUMMARY_BUILDERS = {
    "tollgate": _toll_summary,
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
        *,
        region_names: Mapping[str, str] = STATE_NAMES,
    ) -> None:
        self.projection = projection
        self.places = places
        self.knowledge = knowledge
        self.units = units
        self.clock = clock
        self.region_names = region_names
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
        self._previous_feed: float | None = None
        self._pending_braking: tuple[float, float] | None = None
        self._last_braking = float("-inf")
        self._last_collision = float("-inf")
        self._jam_samples: deque[tuple[float, Snapshot]] = deque()
        self._jam_recovery: float | None = None
        self._jam_reported = False
        self._last_jam = float("-inf")
        self._period: str | None = None
        self._period_candidate: str | None = None
        self._period_count = 0
        self._wipers_since: float | None = None
        self._last_rain = {kind: float("-inf") for kind in ("rain_started", "rain_stopped")}

    def _event(self, kind: str, priority: str, summary: str, **data) -> GameEvent:
        return GameEvent(kind, priority, {**data, "summary": summary}, datetime.now(UTC))

    def feed(self, snapshot: Snapshot) -> list[GameEvent]:
        events = self._transitions(snapshot)
        events.extend(self._close_calls(snapshot))
        events.extend(self._traffic_jam(snapshot))
        if not snapshot.connected:
            self._period = None
            self._period_candidate, self._period_count = None, 0
            self._wipers_since = None
            self._speed_since = None
            self._candidate = None
            self._state_count = 0
            self.previous = snapshot
            return events
        for rule in (self._edges, self._warnings, self._rest, self._deadline, self._speeding):
            events.extend(rule(snapshot))
        if snapshot.paused:
            self._period_candidate, self._period_count = None, 0
            self._wipers_since = None
            self._candidate, self._state_count = None, 0
            self.previous = snapshot
            return events
        lat, lon = self.projection(snapshot.x, snapshot.z)
        events.extend(self._day_period(snapshot, lat))
        events.extend(self._rain(snapshot))
        place = self.places.nearest(lat, lon)
        events.extend(self._state(place))
        events.extend(self._landmark_events(lat, lon))
        events.extend(self._town(place))
        self.previous = snapshot
        return events

    def _day_period(self, s: Snapshot, lat: float) -> list[GameEvent]:
        hour = game_hour(s.game_time)
        current = _game_clock(s.game_time)
        previous = _game_clock(self.previous.game_time)
        if hour is None:
            self._period_candidate, self._period_count = None, 0
            return []
        elevation = sun_elevation(lat, hour)
        period = day_period(elevation, hour)
        jumped = previous is not None and abs(current - previous) > DAY_PERIOD_MAX_JUMP
        if self._period is None or not self.previous.connected or self.previous.paused or jumped:
            self._period = period
            self._period_candidate, self._period_count = None, 0
            return []
        if period == self._period:
            self._period_candidate, self._period_count = None, 0
            return []
        if period != self._period_candidate:
            self._period_candidate, self._period_count = period, 0
        self._period_count += 1
        if self._period_count < DAY_PERIOD_SNAPSHOTS:
            return []
        self._period = period
        self._period_candidate, self._period_count = None, 0
        return [
            self._event(
                "day_period",
                "ambient",
                DAY_PERIOD_SUMMARIES[period],
                period=period,
                game_time=current.strftime("%H:%M"),
                sun_elevation=elevation,
            )
        ]

    def _rain(self, s: Snapshot) -> list[GameEvent]:
        now = self.clock()
        if not self.previous.connected or self.previous.paused:
            self._wipers_since = now if s.wipers_on else None
            return []
        if s.wipers_on == self.previous.wipers_on:
            return []
        if s.wipers_on:
            self._wipers_since = now
            return self._rain_event("rain_started", now)
        since, self._wipers_since = self._wipers_since, None
        if since is None or now - since < RAIN_STOP_MIN_ON_S:
            return []
        return self._rain_event("rain_stopped", now)

    def _rain_event(self, kind: str, now: float) -> list[GameEvent]:
        if now - self._last_rain[kind] < RAIN_COOLDOWN_S:
            return []
        self._last_rain[kind] = now
        summary = {
            "rain_started": "Wipers switched on; it has probably started raining.",
            "rain_stopped": "Wipers switched off; the rain has probably stopped.",
        }[kind]
        return [self._event(kind, "ambient", summary)]

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
        # The first snapshot is the truck's starting state, not news; engine lights (oil, coolant,
        # battery) are normal while the engine is off.
        if not self.previous.connected:
            return []
        events = []
        for kind, flags in WARNING_RULES.items():
            if kind in ENGINE_RUNNING_RULES and not s.engine_on:
                continue
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
        origin = self.region_names.get(previous, previous)
        target = self.region_names.get(place.state, place.state)
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
        if place is None or place.distance_km > 3:
            return []
        if place.land_km2 < 20 and place.population < TOWN_MIN_POPULATION:
            return []
        key = (place.name, place.state)
        if key in self._towns:
            return []
        self._towns.add(key)
        state = self.region_names.get(place.state, place.state)
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
        previous = self.previous
        if (
            previous.connected
            and previous.rest_remaining is not None
            and remaining - previous.rest_remaining >= REST_RESET_MIN
        ):
            self._rest_warned = False
            summary, data = _rested_summary(s, previous, self.units)
            return [self._event("rested", "notable", summary, **data)]
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
                rest_text(remaining) + ".",
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
        if _on_time(s):
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

    def _close_calls(self, s: Snapshot) -> list[GameEvent]:
        now = self.clock()
        previous_time, self._previous_feed = self._previous_feed, now
        if not s.connected or s.paused or not self.previous.connected or self.previous.paused:
            self._pending_braking = None
            return []
        if previous_time is not None and now > previous_time:
            self._braking(s, now, now - previous_time)
        collision = self._collision(s, now)
        if collision:
            return collision
        pending = self._pending_braking
        if pending is None or now - pending[0] <= BRAKING_COLLISION_WINDOW_S:
            return []
        self._pending_braking = None
        self._last_braking = now
        return [
            self._event(
                "hard_braking",
                "notable",
                "Hard braking detected.",
                deceleration_mps2=pending[1],
            )
        ]

    def _braking(self, s: Snapshot, now: float, delta: float) -> None:
        if self._pending_braking is not None or now - self._last_braking < BRAKING_COOLDOWN_S:
            return
        if self.previous.speed < BRAKING_MIN_SPEED_KMH:
            return
        deceleration = (self.previous.speed - s.speed) / 3.6 / delta
        if deceleration < BRAKING_DECELERATION_MPS2:
            return
        self._pending_braking = (now, deceleration)

    def _collision(self, s: Snapshot, now: float) -> list[GameEvent]:
        before, after = self.previous.trailer_damage, s.trailer_damage
        if not after.slots or before.slots != after.slots:
            return []
        increase = max(
            after.cargo - before.cargo, after.body - before.body, after.chassis - before.chassis
        )
        # Subtracting existing wear can round an exact half-percent jump down.
        if increase < COLLISION_DAMAGE_THRESHOLD and not math.isclose(
            increase, COLLISION_DAMAGE_THRESHOLD, abs_tol=1e-12
        ):
            return []
        pending = self._pending_braking
        hard_braking = pending is not None and now - pending[0] <= BRAKING_COLLISION_WINDOW_S
        if hard_braking:
            self._pending_braking = None
        if now - self._last_collision < COLLISION_COOLDOWN_S:
            return []
        self._last_collision = now
        percent = increase * CARGO_DAMAGE_TO_PERCENT
        cargo_damaged = after.cargo > before.cargo
        return [
            self._event(
                "collision",
                "critical",
                f"Collision: trailer damage increased by {percent:g}%.",
                damage_increase_percent=percent,
                cargo_damaged=cargo_damaged,
                hard_braking=hard_braking,
            )
        ]

    def _reset_jam(self) -> None:
        self._jam_samples.clear()
        self._jam_recovery = None
        self._jam_reported = False

    def _traffic_jam(self, s: Snapshot) -> list[GameEvent]:
        now = self.clock()
        if (
            not s.connected
            or s.paused
            or not s.engine_on
            or s.park_brake_on
            or s.speed_limit < JAM_MIN_LIMIT_KMH
            or s.nav_distance_km <= JAM_MIN_DISTANCE_KM
        ):
            self._reset_jam()
            return []
        if s.speed <= s.speed_limit * JAM_RECOVERY_RATIO:
            self._jam_recovery = None
        if s.speed > s.speed_limit * JAM_RECOVERY_RATIO and self._jam_recovery is None:
            self._jam_recovery = now
        if self._jam_recovery is not None and now - self._jam_recovery >= JAM_RECOVERY_S:
            self._reset_jam()
        samples = self._jam_samples
        if samples and now <= samples[-1][0]:
            return []
        samples.append((now, s))
        cutoff = now - JAM_WINDOW_S
        while len(samples) > 1 and samples[1][0] <= cutoff:
            samples.popleft()
        if now - samples[0][0] < JAM_WINDOW_S or self._jam_reported:
            return []
        average, low_time, stops = self._jam_statistics(cutoff)
        if low_time < JAM_WINDOW_S * JAM_LOW_TIME_RATIO:
            return []
        if stops < JAM_MIN_STOPS and average >= JAM_AVERAGE_KMH:
            return []
        if now - self._last_jam < JAM_COOLDOWN_S:
            return []
        self._last_jam, self._jam_reported = now, True
        return [self._jam_event(s, average)]

    def _jam_statistics(self, cutoff: float) -> tuple[float, float, int]:
        samples = list(self._jam_samples)
        distance, low_time, stops = 0.0, 0.0, 0
        for (start, sample), (end, following) in pairwise(samples):
            duration = end - max(start, cutoff)
            distance += sample.speed * duration
            low_time += duration * (sample.speed < sample.speed_limit * JAM_LOW_SPEED_RATIO)
            stops += sample.speed >= JAM_STOP_KMH and following.speed < JAM_STOP_KMH
        return distance / JAM_WINDOW_S, low_time, stops

    def _jam_event(self, s: Snapshot, average: float) -> GameEvent:
        lat, lon = self.projection(s.x, s.z)
        place = self.places.nearest(lat, lon)
        data = {
            "average_speed": self.units.speed(average),
            "average_speed_kmh": average,
            "speed_limit": self.units.speed(s.speed_limit),
            "minutes_stuck": JAM_WINDOW_S / 60,
        }
        if place is not None:
            data.update(name=place.name, region=self.region_names.get(place.state, place.state))
        if (
            place is not None
            and place.distance_km <= JAM_CITY_RADIUS_KM
            and place.population >= JAM_CITY_POPULATION
        ):
            data["population"] = place.population
        initial_eta = self._jam_samples[0][1].nav_estimated
        if initial_eta is not None and s.nav_estimated is not None:
            data["nav_estimated_growth_s"] = max(
                0.0, (s.nav_estimated - initial_eta).total_seconds()
            )
        return self._event(
            "traffic_jam",
            "notable",
            f"Traffic jam: averaging {data['average_speed']} in a {data['speed_limit']} zone "
            f"for {data['minutes_stuck']:g} minutes.",
            **data,
        )

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
