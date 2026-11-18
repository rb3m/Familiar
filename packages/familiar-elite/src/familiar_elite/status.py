"""Retained valid snapshots with acquisition times; partial writes never clear state."""

import json
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from time import monotonic

from familiar.interfaces import load_data


@dataclass(frozen=True)
class Snapshot:
    data: dict
    acquired_at: float


class StatusReader:
    def __init__(self, directory: Path | None, clock=monotonic):
        self.directory = directory
        self.clock = clock
        self.snapshots: dict[str, Snapshot] = {}
        self.signatures = {}
        self.flag_bits = load_data("familiar_elite", "status_flags").value
        self.files = load_data("familiar_elite", "paths").value["snapshot_files"]

    def poll(self):
        for name in self.files:
            self._read(name)

    def _read(self, name):
        if self.directory is None:
            return
        try:
            path = self.directory / self.files[name]
            contents = path.read_bytes()
            if contents == self.signatures.get(name):
                # The game rewrites these files only on change: unchanged still means current.
                self.snapshots[name] = Snapshot(self.snapshots[name].data, self.clock())
                return
            data = json.loads(contents.decode("utf-8-sig"))
        except (OSError, UnicodeError, ValueError):
            return
        if not isinstance(data, dict) or not self._valid(name, data):
            return
        self.signatures[name] = contents
        self.snapshots[name] = Snapshot(data, self.clock())

    @staticmethod
    def _valid(name, data):
        required = {
            "Status": ("Flags", int),
            "NavRoute": ("Route", list),
            "Cargo": ("Count", (int, float)),
        }
        key, kind = required[name]
        if not isinstance(data.get(key), kind):
            return False
        if name != "Status":
            return True
        return isinstance(data.get("Flags2", 0), int) and isinstance(data.get("Fuel", {}), dict)

    @property
    def data(self):
        sample = self.snapshots.get("Status")
        return sample.data if sample else {}

    @property
    def acquired_at(self):
        sample = self.snapshots.get("Status")
        return sample.acquired_at if sample else 0.0

    @property
    def flags(self):
        decoded = map(self._decode_flags, self.flag_bits.items())
        return dict(chain.from_iterable(map(dict.items, decoded)))

    def _decode_flags(self, entry):
        field, bits = entry
        return {name: bool(self.data.get(field, 0) & (1 << bit)) for name, bit in bits.items()}

    def fresh(self, max_age_s=10.0):
        return "Status" in self.snapshots and self.clock() - self.acquired_at <= max_age_s

    @property
    def pips(self):
        """Raw half-pip integers in SYS, ENG, WEP order."""
        return self.data.get("Pips")

    @property
    def fuel_main(self):
        return self.data.get("Fuel", {}).get("FuelMain")

    @property
    def fuel_reservoir(self):
        return self.data.get("Fuel", {}).get("FuelReservoir")

    @property
    def gui_focus(self):
        return self.data.get("GuiFocus")

    @property
    def legal_state(self):
        return self.data.get("LegalState")

    @property
    def cargo(self):
        if "Cargo" in self.data:
            return self.data["Cargo"]
        sample = self.snapshots.get("Cargo")
        return sample.data.get("Count") if sample else None

    @property
    def cargo_inventory(self):
        sample = self.snapshots.get("Cargo")
        if sample is None:
            return []
        return [item for item in sample.data.get("Inventory", []) if isinstance(item, dict)]

    @property
    def destination(self):
        return self.data.get("Destination")

    def jumps_remaining(self, system):
        sample = self.snapshots.get("NavRoute")
        if sample is None:
            return None
        route = sample.data["Route"]
        indices = [
            i
            for i, stop in enumerate(route)
            if isinstance(stop, dict) and stop.get("StarSystem") == system
        ]
        if not indices:
            return None
        return len(route) - indices[0] - 1
