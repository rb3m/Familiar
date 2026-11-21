"""Read desk snapshots and own a helper only when needed."""

import asyncio
import fcntl
import json
import logging
import sys
from collections.abc import Callable
from datetime import datetime, timedelta

from familiar.interfaces import PLUGIN_API_VERSION
from familiar.interfaces.service import CompanionService, ServiceTool, SpeechState
from familiar.interfaces.types import GameEvent

from familiar_desk.feed import utc_now
from familiar_desk.paths import feed_path, lock_path


class DeskService(CompanionService):
    name = "desk"
    api_version = PLUGIN_API_VERSION
    capabilities = frozenset()

    def __init__(self, settings: dict, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.settings = settings
        self.clock = clock
        self.started = clock()
        self._seen = set()
        self._snapshot = None
        self._child = None
        self._started = False
        self._closed = asyncio.Event()

    async def _start(self) -> None:
        with lock_path().open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            # Release before spawn; competing children arbitrate with the same lock.
        command = [sys.executable, "-m", "familiar", "desk-feed"]
        if self.settings.get("config_path"):
            command.extend(["--config", str(self.settings["config_path"])])
        self._child = await asyncio.create_subprocess_exec(*command)

    def _read(self, *, consume: bool = True) -> list[GameEvent]:
        try:
            snapshot = json.loads(feed_path().read_text())
            generated = datetime.fromisoformat(snapshot["generated_at"])
            if snapshot["schema_version"] != 1 or generated.utcoffset() is None:
                return []
            self._snapshot = snapshot
            if not consume:
                return []
            result = []
            now = self.clock()
            for event in snapshot["events"]:
                stamp = datetime.fromisoformat(event["ts"])
                if event["id"] in self._seen:
                    continue
                if stamp < self.started - timedelta(seconds=60):
                    continue
                if now >= stamp + timedelta(seconds=event["ttl_s"]):
                    continue
                self._seen.add(event["id"])
                result.append(
                    GameEvent(
                        kind=event["kind"],
                        priority=event["priority"],
                        timestamp=stamp,
                        data={
                            "summary": event["summary"],
                            "ttl_s": event["ttl_s"],
                            "companion_service": True,
                        },
                    )
                )
            return result
        except (OSError, ValueError, KeyError, TypeError):
            return []

    async def events(self):
        if self._closed.is_set():
            return
        if not self._started:
            self._started = True
            try:
                await self._start()
            except OSError:
                logging.getLogger(__name__).exception("Desk feed helper unavailable")
        while not self._closed.is_set():
            for event in self._read():
                yield event
            try:
                await asyncio.wait_for(self._closed.wait(), self.settings.get("poll_s", 5))
            except TimeoutError:
                continue

    def describe(self) -> str:
        self._read(consume=False)
        if self._snapshot is None:
            return ""
        try:
            entry = self._snapshot["sources"]["clock"]
            observed = datetime.fromisoformat(entry["observed_at"])
            if entry["status"] != "ok":
                return ""
            if (self.clock() - observed).total_seconds() > 3 * self.settings.get("interval_s", 60):
                return ""
            data = entry["data"]
            local = datetime.fromisoformat(data["local_time"])
            detail = data["period"].replace("_", " ")
            if data.get("sunset"):
                sunset = datetime.fromisoformat(data["sunset"])
                verb = "was" if local >= sunset else "is at"
                detail += f"; sunset {verb} {sunset:%H:%M}"
            return f"Local time: {data['weekday']} {local:%Y-%m-%d %H:%M} ({detail})."
        except (ValueError, KeyError, TypeError):
            return ""

    def service_tools(self) -> list[ServiceTool]:
        return []

    async def speech_state(self, state: SpeechState) -> None:
        pass

    async def aclose(self) -> None:
        self._closed.set()
        child = self._child
        if child is None or child.returncode is not None:
            return
        try:
            child.terminate()
            await asyncio.wait_for(child.wait(), 3)
        except TimeoutError:
            child.kill()
            await child.wait()
        except ProcessLookupError:
            await child.wait()
