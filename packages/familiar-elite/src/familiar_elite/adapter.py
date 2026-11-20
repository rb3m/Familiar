"""Elite telemetry and guarded keyboard actions with status/journal read-back."""

import asyncio
import math
from collections import deque
from threading import Lock
from time import monotonic

from familiar.interfaces import (
    PLUGIN_API_VERSION,
    ActionSpec,
    ConfigError,
    Keyboard,
    WatchSample,
    WatchSpec,
    load_data,
)

from .actions import BindingSource, action_specs, hold_times, load_catalogue
from .events import EventDetector
from .exploration import Explorer, local_body, query_specs
from .journal import JournalTailer
from .paths import find_paths
from .state import EliteState
from .status import StatusReader

READBACK_TIMEOUT_S = 2.0
DOCKING_TIMEOUT_S = 10.0
READBACK_STEP_S = 0.1


def _result(status, detail, **values):
    return {"status": status, "detail": detail, **values}


class EliteAdapter:
    game = "elite"
    name = "elite"
    display_name = "Elite Dangerous"
    icon = "🚀"
    api_version = PLUGIN_API_VERSION
    capabilities = frozenset({"events", "snapshot"})

    def __init__(self, settings: dict):
        self.interval_s = float(settings.get("poll_interval_s", 0.5))
        if not math.isfinite(self.interval_s) or self.interval_s <= 0:
            raise ConfigError("poll_interval_s must be positive and finite")
        self.settings = dict(settings)
        self.paths = find_paths(settings)
        self.state = EliteState()
        self.status = StatusReader(self.paths.journal_dir)
        self._status_lock = Lock()
        self.journal = JournalTailer(self.paths.journal_dir, self.interval_s)
        self.detector = EventDetector()
        self.closed = False
        self._catalogue = load_catalogue()
        self._hold_s = hold_times(self._catalogue, settings.get("hold_s", {}))
        self._binding_source = BindingSource(self.paths)
        self._keyboard: Keyboard | None = None
        self._action_lock = asyncio.Lock()
        self._explorer = Explorer(self.settings)

    def attach_keyboard(self, keyboard: Keyboard) -> None:
        self._keyboard = keyboard

    def actions(self) -> dict[str, ActionSpec]:
        return {**action_specs(self._catalogue), **query_specs()}

    def action_context(self) -> str:
        return f"{self.state.commander}:{self.state.ship_id}"

    def _action_guard(self, entry, docking=False):
        if self._keyboard is None:
            return "No keyboard attached"
        if not self.status.fresh():
            return "Elite status is stale or unavailable"
        if self.state.shutdown or self.closed:
            return "Elite is shut down"
        if self.status.gui_focus != 0:
            return "Elite GuiFocus must be 0; close panels and menus"
        flags = self.status.flags
        suit_action = "on_foot_control" in entry and flags.get("on_foot", False)
        if not suit_action and (not flags.get("in_main_ship") or flags.get("on_foot")):
            return "This action requires the main ship, not on foot or in an SRV/fighter"
        if docking and (flags.get("docked") or self.state.docked):
            return "Cannot request docking while docked"
        if docking and (flags.get("supercruise") or self.state.supercruise):
            return "Cannot request docking in supercruise"
        return None

    def _action_steps(self, entry):
        if "steps" in entry:
            return entry["steps"]
        control = entry["control"]
        if self.status.flags.get("on_foot"):
            control = entry.get("on_foot_control", control)
        return [{"control": control}]

    def _keys_guard(self, steps, table):
        missing = next((step["control"] for step in steps if not table.get(step["control"])), None)
        if missing:
            return f"{missing} has no keyboard key; run `familiar elite-bindings --write`"
        return None

    async def perform(self, name: str, arguments: dict) -> dict:
        async with self._action_lock:
            return await self._perform(name, arguments)

    async def _perform(self, name, arguments):
        if name in query_specs():
            return await self._explorer.perform(name, arguments, self.state)
        entry = self._catalogue.get(name)
        if entry is None:
            return _result("error", f"Unknown Elite action: {name}")
        states = entry.get("states", {})
        expected = {"state"} if states else set()
        if set(arguments) != expected or (states and arguments.get("state") not in states):
            return _result(
                "error",
                f"{name}: expected state in {list(states)}"
                if states
                else f"{name} takes no arguments",
            )
        reason = self._action_guard(entry, name == "request_docking")
        if reason:
            return _result("unavailable", reason)
        steps = self._action_steps(entry)
        table = await asyncio.to_thread(self._binding_source.refresh, self.paths)
        reason = self._action_guard(entry, name == "request_docking")
        if reason:
            return _result("unavailable", reason)
        reason = self._keys_guard(steps, table)
        if reason:
            return _result("unavailable", reason)
        if states and self.status.flags[entry["flag"]] == states[arguments["state"]]:
            return _result("already", f"{name} is already {arguments['state']}")
        context = self.action_context()
        docking_count = self.state.docking_event_count
        try:
            for step in steps:
                await self._press_step(step, table, self._hold_s[name])
        except (OSError, ValueError) as exc:
            return _result("unknown", f"Key sequence interrupted; not retried: {exc}")
        if name == "request_docking":
            return await self._await_docking(docking_count, context)
        desired = states.get(arguments.get("state"))
        await self._wait_for(lambda: self._matches(entry, desired), READBACK_TIMEOUT_S)
        if context != self.action_context() or self._action_guard(entry):
            return _result("unknown", "Keys pressed; game context/status changed")
        if self._matches(entry, desired):
            return _result("done", f"{name} confirmed by the ship")
        return _result("unknown", f"Keys pressed; {name} not verified; not repeated")

    async def _await_docking(self, docking_count, context):
        await self._wait_for(
            lambda: self._docking_result(docking_count) is not None, DOCKING_TIMEOUT_S
        )
        if context != self.action_context() or self.state.shutdown or self.closed:
            return _result("unknown", "Docking keys pressed; game context changed")
        return self._docking_result(docking_count) or _result(
            "unknown", "No docking outcome within 10 seconds; request not repeated"
        )

    async def _press_step(self, step, table, hold_s):
        await self._keyboard.tap(table[step["control"]], hold_s=hold_s)
        await asyncio.sleep(step.get("wait_s", 0))

    def _matches(self, entry, desired):
        if "flag" in entry:
            return self.status.flags.get(entry["flag"]) == desired
        return self.status.pips == entry["pips"]

    async def _wait_for(self, predicate, timeout):
        deadline = monotonic() + timeout
        while not predicate() and monotonic() < deadline:
            await asyncio.sleep(READBACK_STEP_S)
            await asyncio.to_thread(self._poll_status)

    def _poll_status(self):
        """Action read-back and the event poller share one snapshot reader."""
        with self._status_lock:
            self.status.poll()

    def _docking_result(self, previous_count):
        if self.state.docking_event_count <= previous_count:
            return None
        event = self.state.last_docking_event
        match event.get("event"):
            case "DockingGranted":
                pad = event.get("LandingPad")
                return _result("done", f"Docking granted; landing pad {pad}", landing_pad=pad)
            case "DockingDenied":
                reason = event.get("Reason", "Unknown reason")
                return _result("done", f"Docking denied: {reason}", reason=reason)
            case "DockingTimeout" | "DockingCancelled":
                return _result("done", event["event"])
        return None

    def rescan(self):
        """Refresh discovered paths; data overrides are reread by find_paths."""
        paths = find_paths(self.settings)
        if paths.journal_dir != self.paths.journal_dir:
            self.state = EliteState()
            self.status = StatusReader(paths.journal_dir)
            self.journal = JournalTailer(paths.journal_dir, self.interval_s)
            self.detector = EventDetector()
        self.paths = paths
        self._binding_source = BindingSource(paths)

    def _poll(self):
        records = self.journal.poll()
        result = []
        for record in records:
            if record.data.get("event") == "LoadGame":
                self.detector = EventDetector()
            self.state.apply(record.data)
            result.extend(self.detector.journal(record.data, self.state, record.historical))
        self._poll_status()
        if self.status.fresh() and not self.state.shutdown:
            result.extend(self.detector.status(self.status.flags))
        return result

    async def events(self):
        pending = deque()
        try:
            while not self.closed:
                if pending:
                    yield pending.popleft()
                    continue
                pending.extend(await asyncio.to_thread(self._poll))
                await asyncio.sleep(self.interval_s)
        finally:
            await self.aclose()

    def _connected(self):
        return self.status.fresh() and not self.state.shutdown and self.state.alive

    async def snapshot(self):
        return {
            **self.state.to_dict(),
            "status": dict(self.status.data),
            "flags": self.status.flags,
            "connected": self._connected(),
            "paused": False,
            "driving": self.watch_sample().valid,
        }

    def describe(self):
        if not self._connected():
            return ""
        state = self.state
        identity = "; ".join(
            str(value) for value in (state.ship, state.ship_name, state.ship_ident) if value
        )
        lines = [
            text
            for text in (
                f"Commander {state.commander}" if state.commander else "",
                f"Ship: {identity}" if identity else "",
                self._ship_description(),
                self._cargo_description(),
                self._system_description(),
                self._scan_description(),
                self._station_description(),
            )
            if text
        ]
        timestamp = max(state.timestamp or "", self.status.data.get("timestamp", ""))
        if timestamp:
            lines.append(f"Game time (UTC): {timestamp}")
        jumps = self.status.jumps_remaining(state.system)
        if jumps is not None:
            lines.append(f"Route jumps remaining: {jumps}")
        active = ", ".join(key for key, enabled in self.status.flags.items() if enabled)
        if active:
            lines.append(f"Active flags: {active}")
        return "\n".join(lines)

    def _ship_description(self):
        state, status = self.state, self.status
        shields = "not fitted"
        if state.shields_fitted is not False:
            shields = {True: "up", False: "down"}[status.flags["shields_up"]]
        parts = [f"Shields: {shields}"]
        if state.hull is not None:
            parts.append(f"hull {100 * state.hull:g} percent")
        fuel = status.fuel_main
        if fuel is None:
            fuel = state.fuel_level
        if fuel is not None:
            parts.append(self._fuel_description(fuel))
        if state.max_jump_range is not None:
            parts.append(f"max jump range {state.max_jump_range:g} light-years")
        balance = status.data.get("Balance")
        if balance is not None:
            parts.append(f"balance {balance} credits")
        if status.legal_state is not None:
            parts.append(f"legal state {status.legal_state}")
        if status.pips is not None:
            pips = "/".join(f"{value / 2:g}" for value in status.pips)
            parts.append(f"pips SYS/ENG/WEP {pips}")
        if status.flags.get("over_heating") and status.data.get("Heat") is not None:
            parts.append(f"heat {status.data['Heat']:g} percent (overheating)")
        return "; ".join(parts)

    def _fuel_description(self, fuel):
        capacity = self.state.fuel_capacity
        if capacity is None or capacity <= 0:
            return f"fuel {fuel:g} tonnes"
        return f"fuel {fuel:g} / {capacity:g} tonnes ({100 * fuel / capacity:g} percent)"

    def _cargo_description(self):
        amount, capacity = self.status.cargo, self.state.cargo_capacity
        parts = []
        if amount is not None:
            parts.append(f"{amount:g} tonnes used")
        if capacity is not None:
            parts.append(f"capacity {capacity:g} tonnes")
        inventory = sorted(self.status.cargo_inventory, key=lambda item: -item.get("Count", 0))
        parts.extend(self._cargo_item(item) for item in inventory[:5])
        if len(inventory) > 5:
            parts.append(f"{len(inventory) - 5} more cargo types")
        if not parts:
            return ""
        return "Cargo: " + "; ".join(parts)

    @staticmethod
    def _cargo_item(item):
        name = (
            item.get("Name_Localised")
            or item.get("name_Localised")
            or item.get("Name", "unnamed cargo")
        )
        flags = [
            text
            for enabled, text in (
                (item.get("Stolen"), f"stolen {item.get('Stolen')} tonnes"),
                (item.get("MissionID") or item.get("Mission"), "mission cargo"),
            )
            if enabled
        ]
        suffix = f" ({', '.join(flags)})" if flags else ""
        return f"{name}: {item.get('Count', 0):g} tonnes{suffix}"

    def _system_description(self):
        state = self.state
        fields = {
            "System": state.system,
            "body": state.body,
            "star class": state.star_class,
            "allegiance": state.allegiance,
            "economy": state.economy,
            "security": state.security,
            "government": state.government,
            "population": state.population,
            "controlling faction": (state.faction or {}).get("Name"),
            "FSS body count": state.bodies,
        }
        return "; ".join(f"{key}: {value}" for key, value in fields.items() if value is not None)

    def _scan_description(self):
        scans = self.state.scans
        if not scans:
            return ""
        interest = load_data("familiar_elite", "interest").value
        bodies = sorted(
            (local_body(scan, interest) for scan in scans.values()),
            key=lambda body: (not bool(body["notable"]), not body["first_discovery"]),
        )
        features = [
            self._scan_feature(body)
            for body in bodies
            if body["notable"] or body["landable"] or body["first_discovery"]
        ]
        counts = (
            f"Our scans: {len(scans)} bodies; "
            f"{sum(bool(body['landable']) for body in bodies)} landable; "
            f"{sum(body['first_discovery'] for body in bodies)} first discoveries"
        )
        if not features:
            return counts
        return counts + "; notable: " + ", ".join(features[:5])

    @staticmethod
    def _scan_feature(body):
        details = [
            text
            for text in (
                body["notable"],
                "landable" if body["landable"] else "",
                "first discovery" if body["first_discovery"] else "",
            )
            if text
        ]
        return f"{body['name']} ({', '.join(details)})"

    def _station_description(self):
        state = self.state
        if not state.docked:
            return ""
        fields = {
            "Docked": state.station,
            "type": state.station_type,
            "economy": state.station_economy,
        }
        parts = [f"{key}: {value}" for key, value in fields.items() if value is not None]
        if state.station_services is not None:
            parts.append("services: " + ", ".join(state.station_services))
        return "; ".join(parts)

    def watchables(self):
        return {
            "fuel": WatchSpec("%", "main fuel tank remaining"),
            "cargo": WatchSpec("count", "cargo units aboard"),
            "jumps_remaining": WatchSpec("count", "jumps remaining in plotted route"),
        }

    def watch_sample(self):
        flags = self.status.flags
        valid = (
            self._connected()
            and flags.get("in_main_ship", False)
            and not flags.get("on_foot", False)
        )
        amount, capacity = self.status.fuel_main, self.state.fuel_capacity
        fuel = None
        if amount is not None and capacity is not None and capacity > 0:
            fuel = 100 * amount / capacity
        values = {
            "fuel": fuel,
            "cargo": self.status.cargo,
            "jumps_remaining": self.status.jumps_remaining(self.state.system),
        }
        return WatchSample(
            values,
            f"{self.state.commander}:{self.state.ship_id}",
            valid,
            False,
            self.status.acquired_at,
        )

    def tools(self):
        return []

    async def aclose(self):
        self.closed = True
        await self.journal.aclose()
        await self._explorer.aclose()
