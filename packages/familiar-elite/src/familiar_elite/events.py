"""Journal and status transitions with per-kind monotonic cooldowns."""

from datetime import UTC, datetime
from time import monotonic

from familiar.interfaces import GameEvent, load_data

# Tuning owned by Familiar, independent of Frontier data.
COOLDOWNS = {
    "system_arrival": 5.0,
    "scoopable_star": 30.0,
    "low_fuel": 0.0,
    "overheating": 30.0,
    "shields_down": 10.0,
    "shields_up": 10.0,
    "interdiction": 10.0,
    "docking_granted": 2.0,
    "docking_denied": 2.0,
    "docked": 5.0,
    "undocked": 5.0,
    "first_discovery": 10.0,
    "discovery_scan": 10.0,
    "hull_damage": 10.0,
}
CRITICAL = {"low_fuel", "overheating", "shields_down", "interdiction", "hull_damage"}
HULL_THRESHOLDS = (0.75, 0.50, 0.25, 0.10)


class EventDetector:
    def __init__(self, clock=monotonic):
        self.clock = clock
        self.cooldowns = {}
        self.previous_flags = None
        self.hull = None
        self.exploration = load_data("familiar_elite", "exploration").value

    def _emit(self, kind, summary, **data):
        now = self.clock()
        if now - self.cooldowns.get(kind, float("-inf")) < COOLDOWNS[kind]:
            return []
        self.cooldowns[kind] = now
        priority = "critical" if kind in CRITICAL else "notable"
        return [
            GameEvent(
                kind, priority, {"summary": summary, "ttl_s": 30.0, **data}, datetime.now(UTC)
            )
        ]

    def journal(self, event, state, historical=False):
        if event.get("event") == "HullDamage":
            return self._hull(event.get("Health"), historical)
        if historical:
            return []
        match event.get("event"):
            case "FSDJump":
                details = "; ".join(
                    str(value)
                    for value in (state.security, state.economy, state.allegiance)
                    if value
                )
                return self._emit(
                    "system_arrival",
                    f"Arrived in {state.system}: {details}; population {state.population}.",
                    system=state.system,
                )
            case "StartJump":
                if event.get("StarClass") not in self.exploration["scoopable_stars"]:
                    return []
                return self._emit(
                    "scoopable_star", f"Scoopable class {event['StarClass']} star ahead."
                )
            case "ShieldState":
                return self._shields(event.get("ShieldsUp"))
            case "Interdicted":
                return self._emit("interdiction", "The ship is being interdicted.")
            case "DockingGranted":
                return self._emit(
                    "docking_granted",
                    f"Docking granted at {state.station}, pad {state.landing_pad}.",
                )
            case "DockingDenied":
                return self._emit(
                    "docking_denied",
                    f"Docking denied: {state.docking_reason}.",
                    reason=state.docking_reason,
                )
            case "Docked":
                return self._emit("docked", f"Docked at {state.station}.")
            case "Undocked":
                return self._emit("undocked", f"Undocked from {state.station}.")
            case "Scan":
                if event.get("WasDiscovered") is not False:
                    return []
                if event.get("PlanetClass") not in self.exploration["high_value_classes"]:
                    return []
                return self._emit(
                    "first_discovery",
                    f"First discovery scan: {event.get('BodyName')}, {event.get('PlanetClass')}.",
                )
            case "FSSDiscoveryScan":
                return self._emit(
                    "discovery_scan",
                    f"Discovery scan detected {event.get('BodyCount')} bodies in {state.system}.",
                )
        return []

    def _hull(self, health, historical):
        previous, self.hull = self.hull, health
        if historical or health is None:
            return []
        baseline = 1.0 if previous is None else previous
        if not any(health <= threshold < baseline for threshold in HULL_THRESHOLDS):
            return []
        return self._emit("hull_damage", f"Hull integrity down to {health:.0%}.", health=health)

    def _shields(self, up):
        if up is None:
            return []
        kind = "shields_up" if up else "shields_down"
        label = {True: "up", False: "down"}[up]
        return self._emit(kind, f"Shields {label}.")

    def status(self, flags, historical=False):
        previous, self.previous_flags = self.previous_flags, dict(flags)
        if historical or previous is None:
            return []
        result = []
        for flag, kind, summary in (
            ("low_fuel", "low_fuel", "Fuel level is low."),
            ("over_heating", "overheating", "The ship is overheating."),
            ("being_interdicted", "interdiction", "The ship is being interdicted."),
        ):
            if flags.get(flag) and not previous.get(flag):
                result.extend(self._emit(kind, summary))
        if flags.get("shields_up") != previous.get("shields_up"):
            result.extend(self._shields(flags.get("shields_up")))
        return result
