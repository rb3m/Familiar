"""Bounded background sampling with immediate snapshot-only lookups."""

import asyncio
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime

from familiar.interfaces import PLUGIN_API_VERSION
from familiar.interfaces.service import CompanionService, ServiceTool, SpeechState
from familiar.interfaces.types import GameEvent, ToolSpec

from familiar_system.alerts import AlertTracker
from familiar_system.readers import READERS, Reader, Resources
from familiar_system.sensors import MatchedTemperature, reported, settings_for

_LOG = logging.getLogger(__name__)
ASPECTS = ("overview", "thermal", "storage", "memory", "load")


def _available(value):
    if value is None:
        return "unavailable"
    return value


@dataclass(frozen=True)
class Snapshot:
    value: object
    observed_at: datetime


class SystemService(CompanionService):
    name = "system"
    api_version = PLUGIN_API_VERSION
    capabilities = frozenset()

    def __init__(self, settings: dict, *, reader: Reader | None = None) -> None:
        self.settings = settings_for(settings)
        factory = READERS.get(sys.platform)
        self.reader = reader
        if reader is None and factory is not None:
            self.reader = factory(self.settings)
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="familiar-system")
        self._inflight: dict[str, asyncio.Future] = {}
        self._tasks: list[asyncio.Task] = []
        self._events: asyncio.Queue[GameEvent | None] = asyncio.Queue()
        self.snapshots: dict[str, Snapshot] = {}
        self.alerts = AlertTracker(self.settings)
        self._closed = False

    async def _read(self, kind: str, function):
        pending = self._inflight.get(kind)
        if self._closed or (pending is not None and not pending.done()):
            return None
        future = asyncio.get_running_loop().run_in_executor(self._executor, function)
        self._inflight[kind] = future
        # Retrieve even late failures; a timed-out result is never published.
        future.add_done_callback(self._consume)
        try:
            return await asyncio.wait_for(
                asyncio.shield(future), timeout=self.settings["read_timeout_s"]
            )
        except (TimeoutError, OSError, ValueError):
            _LOG.debug("System %s sample unavailable", kind, exc_info=True)
            return None

    @staticmethod
    def _consume(future: asyncio.Future) -> None:
        if not future.cancelled():
            future.exception()

    async def _sample(self, kind: str) -> None:
        if self.reader is None:
            return
        functions = {"thermal": self.reader.temperatures, "storage": self.reader.resources}
        result = await self._read(kind, functions[kind])
        if result is None or self._closed:
            return
        now = datetime.now(UTC)
        if kind == "thermal":
            result = reported(result, self.settings)
        self.snapshots[kind] = Snapshot(result, now)
        arguments = {"temperatures": result}
        if kind == "storage":
            arguments = {"filesystems": result.filesystems}
        event = self.alerts.sample(timestamp=now, **arguments)
        if event is not None:
            self._events.put_nowait(event)

    async def _poll(self, kind: str) -> None:
        loop = asyncio.get_running_loop()
        interval = self.settings[f"{kind}_interval_s"]
        while not self._closed:
            started = loop.time()
            try:
                await self._sample(kind)
            except Exception:
                _LOG.exception("System %s sample failed", kind)
            await asyncio.sleep(max(0, interval - (loop.time() - started)))

    async def events(self):
        if not self._tasks and not self._closed:
            self._tasks = [asyncio.create_task(self._poll(kind)) for kind in ("thermal", "storage")]
        while not self._closed:
            event = await self._events.get()
            if event is None:
                return
            yield event

    def _metadata(self, kind: str, now: datetime) -> dict:
        snapshot = self.snapshots.get(kind)
        if snapshot is None:
            return {"observed_at": "unavailable", "age_s": "unavailable", "stale": True}
        age = max(0, (now - snapshot.observed_at).total_seconds())
        return {
            "observed_at": snapshot.observed_at.isoformat(),
            "age_s": age,
            "stale": age > 2 * self.settings[f"{kind}_interval_s"],
        }

    def _value(self, value, unit: str, kind: str, now: datetime) -> dict:
        return {"value": _available(value), "unit": unit, **self._metadata(kind, now)}

    def _temperature(self, item: MatchedTemperature, now: datetime) -> dict:
        sensor = item.sensor
        return {
            **self._value(sensor.celsius, "°C", "thermal", now),
            "id": sensor.id or "unavailable",
            "driver": sensor.driver or "unavailable",
            "device": sensor.device or "unavailable",
            "label": sensor.label or "unavailable",
            "role": item.role,
            "line_c": _available(item.line_c),
            "line_source": item.line_source or "unavailable",
        }

    def _thermal(self, now: datetime) -> list[dict] | str:
        snapshot = self.snapshots.get("thermal")
        if snapshot is None or not snapshot.value:
            return "unavailable"
        return [self._temperature(item, now) for item in snapshot.value]

    def _resources(self) -> Resources:
        snapshot = self.snapshots.get("storage")
        if snapshot is None:
            return Resources()
        return snapshot.value

    def _storage(self, now: datetime) -> list[dict] | str:
        filesystems = self._resources().filesystems
        if not filesystems:
            return "unavailable"
        return [
            {
                "mountpoint": fs.mountpoint or "unavailable",
                "fstype": fs.fstype or "unavailable",
                "total": self._value(fs.total_b, "bytes", "storage", now),
                "free": self._value(fs.free_b, "bytes", "storage", now),
                "free_pct": self._value(fs.free_pct, "%", "storage", now),
            }
            for fs in filesystems
        ]

    def _memory(self, now: datetime) -> dict:
        memory = self._resources().memory
        return {
            "total": self._value(memory.total_b, "bytes", "storage", now),
            "available": self._value(memory.available_b, "bytes", "storage", now),
        }

    def _load(self, now: datetime) -> dict:
        resources = self._resources()
        load = resources.load
        return {
            "one": self._value(load.one, "load", "storage", now),
            "five": self._value(load.five, "load", "storage", now),
            "fifteen": self._value(load.fifteen, "load", "storage", now),
            "cpus": self._value(load.cpus, "count", "storage", now),
            "uptime": self._value(resources.uptime_s, "s", "storage", now),
        }

    def _hottest(self, role: str, now: datetime) -> dict:
        snapshot = self.snapshots.get("thermal", Snapshot([], now))
        candidates = [item for item in snapshot.value if item.role == role]
        if not candidates:
            return self._value(None, "°C", "thermal", now)
        return self._temperature(max(candidates, key=lambda item: item.sensor.celsius), now)

    def _overview(self, now: datetime) -> dict:
        storage = self._storage(now)
        tightest = {"value": "unavailable", **self._metadata("storage", now)}
        if isinstance(storage, list):
            known = [fs for fs in storage if fs["free_pct"]["value"] != "unavailable"]
            tightest = min(known, key=lambda fs: fs["free_pct"]["value"], default=tightest)
        return {
            "cpu": self._hottest("cpu", now),
            "gpu": self._hottest("gpu", now),
            # The GPU's limit applies to its hottest point (junction), not the edge it reports.
            "gpu_limit": self._hottest("gpu_limit", now),
            "drive": self._hottest("drive", now),
            "filesystem": tightest,
            "load_one": self._load(now)["one"],
            "memory_available": self._memory(now)["available"],
            "uptime": self._load(now)["uptime"],
        }

    async def system_status(self, aspect: str = "overview") -> dict:
        handlers = {
            "overview": self._overview,
            "thermal": self._thermal,
            "storage": self._storage,
            "memory": self._memory,
            "load": self._load,
        }
        handler = handlers.get(aspect)
        if handler is None:
            return {"status": "invalid_request", "aspects": list(ASPECTS)}
        now = datetime.now(UTC)
        kind = "storage"
        if aspect == "thermal":
            kind = "thermal"
        return {"aspect": aspect, "data": handler(now), **self._metadata(kind, now)}

    def service_tools(self) -> list[ServiceTool]:
        return [
            ServiceTool(
                ToolSpec(
                    "system_status",
                    "Read the last system health snapshot; never waits for a new sample.",
                    {
                        "type": "object",
                        "properties": {
                            "aspect": {
                                "type": "string",
                                "enum": list(ASPECTS),
                                "default": "overview",
                            },
                        },
                        "required": [],
                        "additionalProperties": False,
                    },
                ),
                self.system_status,
            )
        ]

    def describe(self) -> str:
        return ""

    async def speech_state(self, state: SpeechState) -> None:
        pass

    async def aclose(self) -> None:
        self._closed = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._events.put_nowait(None)
