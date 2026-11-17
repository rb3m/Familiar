"""Pure flight transitions with real-time confirmation and resource hysteresis."""

import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from familiar.interfaces import GameEvent

from .telemetry import Snapshot

STAGE_QUIET_S = 1.0  # folds quick multi-staging; 5 s felt late in play
ORBIT_HOLD_S = 3.0
LANDING_HOLD_S = 2.0
WARP_HOLD_S = 3.0
WARP_COOLDOWN_S = 600.0  # players warp constantly
WARP_RATIO = 10.0
AIRLESS_MARGIN_M = 5000.0
REENTRY_MIN_SPEED_MS = 1000.0
LOW_FRACTION = 0.20
CRITICAL_FRACTION = 0.05
RECOVERY_FRACTION = 0.25
CRITICAL_RECOVERY_FRACTION = 0.10
GROUNDED = {"pre_launch", "landed", "splashed"}
AIRBORNE = {"flying", "sub_orbital", "orbiting", "escaping"}
MEMORY_ARRIVALS = {"soi_changed", "landed", "splashed", "orbit_reached"}
SALIENCE = {
    "liftoff": 0.6,
    "staged": 0.2,
    "orbit_reached": 0.7,
    "soi_changed": 0.8,
    "reentry": 0.6,
    "landed": 0.8,
    "splashed": 0.7,
    "eva_started": 0.5,
    "warp_changed": 0.0,
    "low_charge": 0.3,
    "low_fuel": 0.3,
}
EVENT_KINDS = frozenset(SALIENCE)


def orbit_safe(s: Snapshot) -> bool:
    if s.eccentricity is None or s.periapsis_alt_m is None or s.has_atmosphere is None:
        return False
    margin = AIRLESS_MARGIN_M
    if s.has_atmosphere:
        if s.atmosphere_depth_m is None:
            return False
        margin = s.atmosphere_depth_m
    return s.eccentricity < 1 and s.periapsis_alt_m > margin and s.situation in AIRBORNE


def fraction(amount, maximum) -> float | None:
    if amount is None or maximum is None or maximum <= 0:
        return None
    return amount / maximum


def fuel_fraction(s: Snapshot) -> float | None:
    # Whole-vessel liquid fuel: per-stage levels hit zero at every normal burnout.
    return fraction(s.liquid_fuel, s.liquid_fuel_max)


class EventDetector:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.previous: Snapshot | None = None
        self.cooldowns: dict[str, float] = {}
        self.pending: dict[str, tuple[object, float]] = {}
        self.warned: dict[str, int] = {}
        self.orbit_announced = False
        self.airborne = False
        self.warp = None

    def _baseline(self, s: Snapshot) -> None:
        self.pending.clear()
        self.cooldowns.clear()
        self.orbit_announced = orbit_safe(s)
        self.airborne = s.situation in AIRBORNE
        self.warp = s.warp_rate
        self.warned = {
            "low_charge": self._level(fraction(s.electric_charge, s.electric_charge_max)),
            "low_fuel": self._level(fuel_fraction(s)),
        }
        self.previous = s

    @staticmethod
    def _level(ratio) -> int:
        if ratio is None or ratio > LOW_FRACTION:
            return 0
        if ratio <= CRITICAL_FRACTION:
            return 2
        return 1

    def _event(self, kind, summary, priority="notable", **data) -> GameEvent:
        return GameEvent(
            kind,
            priority,
            dict(
                summary=summary,
                facts=[],
                ttl_s=30.0,
                memory_salience=SALIENCE[kind],
                memory_arrival=kind in MEMORY_ARRIVALS,
                **data,
            ),
            datetime.now(UTC),
        )

    def _ready(self, key, value, hold, now) -> bool:
        candidate, since = self.pending.get(key, (None, now))
        if candidate != value:
            self.pending[key] = (value, now)
            return False
        return now - since >= hold

    def _allowed(self, key, now, cooldown=30.0) -> bool:
        if now - self.cooldowns.get(key, float("-inf")) < cooldown:
            return False
        self.cooldowns[key] = now
        return True

    def feed(self, s: Snapshot) -> list[GameEvent]:
        p = self.previous
        reset = (
            p is None
            or not p.in_flight
            or not s.in_flight
            or p.paused
            or s.paused
            or p.scene != s.scene
            or (p.vessel_id or p.vessel_name) != (s.vessel_id or s.vessel_name)
        )
        if reset:
            eva = (
                p is not None
                and p.in_flight
                and s.in_flight
                and not p.paused
                and not s.paused
                and p.scene == s.scene
                and p.vessel_type != "eva"
                and s.vessel_type == "eva"
            )
            self._baseline(s)
            if eva:
                return [
                    self._event(
                        "eva_started",
                        f"{s.vessel_name} started an EVA.",
                        "ambient",
                        kerbal=s.vessel_name,
                    )
                ]
            return []
        now = self.clock()
        events = []
        if p.body != s.body and p.body and s.body:
            self.pending.clear()
            self.orbit_announced = orbit_safe(s)
            events.append(
                self._event("soi_changed", f"Entered {s.body}'s sphere of influence.", body=s.body)
            )
        if (
            p.situation in {"pre_launch", "landed"}
            and s.situation in {"flying", "sub_orbital"}
            and p.altitude_m is not None
            and s.altitude_m is not None
            and s.altitude_m > p.altitude_m
            and self._allowed("liftoff", now)
        ):
            events.append(self._event("liftoff", f"{s.vessel_name} lifted off from {s.body}."))
            p = replace(p, current_stage=s.current_stage)  # the launch staging is the liftoff
        events.extend(self._staging(p, s, now))
        events.extend(self._orbit(s, now))
        if (
            s.has_atmosphere
            and s.atmosphere_depth_m is not None
            and p.altitude_m is not None
            and s.altitude_m is not None
            and p.altitude_m > s.atmosphere_depth_m >= s.altitude_m
            and (s.vertical_speed_ms or 0) < 0
            and max(p.speed_orbital_ms or 0, s.speed_orbital_ms or 0) >= REENTRY_MIN_SPEED_MS
            and p.body == s.body
            and self._allowed("reentry", now)
        ):
            events.append(self._event("reentry", f"Descending into {s.body}'s atmosphere."))
        events.extend(self._landing(s, now))
        events.extend(self._warp(s, now))
        events.extend(self._warnings(p, s))
        self.previous = s
        return events

    def _staging(self, p, s, now) -> list[GameEvent]:
        if p.current_stage is None or s.current_stage is None:
            self.pending.pop("staged", None)
            return []
        if s.current_stage < p.current_stage:
            start = self.pending.get("staged", ((p.current_stage, s.current_stage), now))[0][0]
            self.pending["staged"] = ((start, s.current_stage), now)
        pending = self.pending.get("staged")
        if pending is None or now - pending[1] < STAGE_QUIET_S:
            return []
        (start, end), _ = self.pending.pop("staged")
        return [
            self._event(
                "staged", f"Staged from {start} to {end}.", from_stage=start, current_stage=end
            )
        ]

    def _orbit(self, s, now) -> list[GameEvent]:
        if not orbit_safe(s):
            self.pending.pop("orbit", None)
            self.orbit_announced = False
            return []
        if self.orbit_announced or not self._ready("orbit", s.body, ORBIT_HOLD_S, now):
            return []
        self.orbit_announced = True
        if not self._allowed("orbit_reached", now):
            return []
        return [
            self._event("orbit_reached", f"Reached a stable orbit around {s.body}.", body=s.body)
        ]

    def _landing(self, s, now) -> list[GameEvent]:
        if s.situation in AIRBORNE:
            self.airborne = True
        if s.situation not in {"landed", "splashed"} or not self.airborne:
            self.pending.pop("landing", None)
            return []
        if not self._ready("landing", s.situation, LANDING_HOLD_S, now):
            return []
        self.airborne = False
        if not self._allowed(s.situation, now):
            return []
        location = f"{s.body}, {s.biome}" if s.biome else s.body
        return [
            self._event(
                s.situation,
                f"{s.vessel_name} {s.situation} on {location}.",
                body=s.body,
                biome=s.biome,
            )
        ]

    def _warp(self, s, now) -> list[GameEvent]:
        rate = s.warp_rate
        if rate is None or rate <= 0 or self.warp is None or self.warp <= 0:
            self.warp = rate
            self.pending.pop("warp", None)
            return []
        if max(rate, self.warp) / min(rate, self.warp) < WARP_RATIO:
            self.pending.pop("warp", None)
            return []
        if not self._ready("warp", rate, WARP_HOLD_S, now):
            return []
        self.warp = rate
        if not self._allowed("warp", now, WARP_COOLDOWN_S):
            return []
        return [
            self._event("warp_changed", f"Time warp is now {rate:g}x.", "ambient", warp_rate=rate)
        ]

    def _warnings(self, p, s) -> list[GameEvent]:
        ratios = {
            "low_charge": fraction(s.electric_charge, s.electric_charge_max),
            "low_fuel": fuel_fraction(s),
        }
        if s.situation not in AIRBORNE:
            self.warned = {kind: self._level(ratio) for kind, ratio in ratios.items()}
            return []
        events = []
        for kind, ratio in ratios.items():
            if ratio is None:
                continue
            previous = self.warned.get(kind, 0)
            if ratio > RECOVERY_FRACTION:
                self.warned[kind] = 0
                continue
            if previous == 2 and ratio > CRITICAL_RECOVERY_FRACTION:
                self.warned[kind] = 1
            level = self._level(ratio)
            if level <= self.warned.get(kind, 0):
                continue
            self.warned[kind] = level
            priority = {1: "notable", 2: "critical"}[level]
            label = {"low_charge": "Electric charge", "low_fuel": "Liquid fuel (whole vessel)"}[
                kind
            ]
            events.append(
                self._event(kind, f"{label} low: {ratio:.0%} remaining.", priority, fraction=ratio)
            )
        return events
