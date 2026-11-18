"""Read-only Elite Dangerous adapter: all I/O is local and off the event loop."""

import asyncio
import math
from collections import deque

from familiar.interfaces import PLUGIN_API_VERSION, ConfigError, WatchSample, WatchSpec

from .events import EventDetector
from .journal import JournalTailer
from .paths import find_paths
from .state import EliteState
from .status import StatusReader


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
        self.journal = JournalTailer(self.paths.journal_dir, self.interval_s)
        self.detector = EventDetector()
        self.closed = False

    def rescan(self):
        """Refresh discovered paths; data overrides are reread by find_paths."""
        paths = find_paths(self.settings)
        if paths.journal_dir != self.paths.journal_dir:
            self.state = EliteState()
            self.status = StatusReader(paths.journal_dir)
            self.journal = JournalTailer(paths.journal_dir, self.interval_s)
            self.detector = EventDetector()
        self.paths = paths

    def _poll(self):
        records = self.journal.poll()
        result = []
        for record in records:
            if record.data.get("event") == "LoadGame":
                self.detector = EventDetector()
            self.state.apply(record.data)
            result.extend(self.detector.journal(record.data, self.state, record.historical))
        self.status.poll()
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
        values = self.watch_sample().values
        active = ", ".join(key for key, enabled in self.status.flags.items() if enabled)
        fuel = "unknown"
        if values["fuel"] is not None:
            fuel = f"{values['fuel']:.1f}%"
        cargo = values["cargo"] if values["cargo"] is not None else "unknown"
        jumps = values["jumps_remaining"]
        jumps = jumps if jumps is not None else "unknown"
        return (
            f"Commander {self.state.commander or 'unknown'}; "
            f"ship {self.state.ship_name or self.state.ship or 'unknown'}.\n"
            f"System: {self.state.system or 'unknown'}; "
            f"station/body: {self.state.station or self.state.body or 'unknown'}.\n"
            f"Fuel: {fuel}; cargo: {cargo}; route jumps remaining: {jumps}.\n"
            f"Active flags: {active}."
        )

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
