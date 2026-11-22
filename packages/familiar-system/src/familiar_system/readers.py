"""Blocking, platform-specific reads; unavailable fields remain None."""

import math
import time
from dataclasses import dataclass, field
from itertools import chain
from pathlib import Path
from typing import Protocol

import psutil


@dataclass(frozen=True)
class Temperature:
    id: str | None = None
    driver: str | None = None
    label: str | None = None
    device: str | None = None
    celsius: float | None = None
    max_c: float | None = None
    crit_c: float | None = None


@dataclass(frozen=True)
class Filesystem:
    mountpoint: str | None = None
    fstype: str | None = None
    total_b: int | None = None
    free_b: int | None = None

    @property
    def free_pct(self) -> float | None:
        if self.free_b is None or self.total_b is None or self.total_b <= 0:
            return None
        return self.free_b / self.total_b * 100


@dataclass(frozen=True)
class Memory:
    total_b: int | None = None
    available_b: int | None = None


@dataclass(frozen=True)
class Load:
    one: float | None = None
    five: float | None = None
    fifteen: float | None = None
    cpus: int | None = None


@dataclass(frozen=True)
class Resources:
    filesystems: list[Filesystem] = field(default_factory=list)
    memory: Memory = field(default_factory=Memory)
    load: Load = field(default_factory=Load)
    uptime_s: float | None = None


class Reader(Protocol):
    def temperatures(self) -> list[Temperature]: ...

    def resources(self) -> Resources: ...

    def partitions(self) -> list: ...


def _text(path: Path) -> str | None:
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def _degrees(path: Path) -> float | None:
    try:
        value = float(_text(path)) / 1000
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or not -50 <= value <= 200:
        return None
    return value


def _attempt(function, default=None):
    try:
        return function()
    except (OSError, ValueError, NotImplementedError, psutil.Error):
        return default


class PsutilReader:
    def __init__(self, settings: dict, *, backend=psutil) -> None:
        self.network_fstypes = set(settings["network_fstypes"])
        self.backend = backend

    def temperatures(self) -> list[Temperature]:
        return []

    def partitions(self) -> list:
        return _attempt(lambda: self.backend.disk_partitions(all=False), [])

    def filesystems(self) -> list[Filesystem]:
        # Select before probing: network paths must never reach disk_usage.
        local = [p for p in self.partitions() if p.fstype not in self.network_fstypes]
        selected = {}
        for partition in sorted(local, key=lambda p: (len(p.mountpoint), p.mountpoint)):
            selected.setdefault(partition.device, partition)
        return [self._filesystem(p) for p in selected.values()]

    def _filesystem(self, partition) -> Filesystem:
        usage = _attempt(lambda: self.backend.disk_usage(partition.mountpoint))
        if usage is None:
            return Filesystem(partition.mountpoint, partition.fstype)
        return Filesystem(partition.mountpoint, partition.fstype, usage.total, usage.free)

    def _memory(self) -> Memory:
        memory = _attempt(self.backend.virtual_memory)
        if memory is None:
            return Memory()
        return Memory(memory.total, memory.available)

    def _load(self) -> Load:
        averages = _attempt(self.backend.getloadavg, (None, None, None))
        return Load(*averages, _attempt(self.backend.cpu_count))

    def resources(self) -> Resources:
        boot = _attempt(self.backend.boot_time)
        uptime = None
        if boot is not None:
            uptime = max(0, time.time() - boot)
        return Resources(self.filesystems(), self._memory(), self._load(), uptime)


class LinuxReader(PsutilReader):
    def __init__(self, settings: dict, *, sysfs_root: Path = Path("/sys"), **kwargs) -> None:
        super().__init__(settings, **kwargs)
        self.sysfs_root = Path(sysfs_root)

    @staticmethod
    def _device(hwmon: Path) -> str | None:
        link = hwmon / "device"
        if not link.exists():
            return None
        device = link.resolve()
        model = _text(device / "model")
        if model:
            return f"{device.name} ({model})"
        return device.name

    def _sensor(self, hwmon: Path, sensor: Path) -> Temperature:
        prefix = sensor.name.removesuffix("_input")
        driver = _text(hwmon / "name")
        label = _text(hwmon / f"{prefix}_label") or prefix
        device = self._device(hwmon)
        return Temperature(
            f"{driver}:{device}:{label}",
            driver,
            label,
            device,
            _degrees(sensor),
            _degrees(hwmon / f"{prefix}_max"),
            _degrees(hwmon / f"{prefix}_crit"),
        )

    def _temperatures(self, hwmon: Path) -> list[Temperature]:
        return [self._sensor(hwmon, sensor) for sensor in sorted(hwmon.glob("temp*_input"))]

    def temperatures(self) -> list[Temperature]:
        groups = map(self._temperatures, sorted((self.sysfs_root / "class/hwmon").glob("hwmon*")))
        return list(chain.from_iterable(groups))


READERS = {"linux": LinuxReader, "darwin": PsutilReader}
