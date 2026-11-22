"""Read desk snapshots and own a helper only when needed."""

import asyncio
import fcntl
import json
import logging
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from familiar.interfaces import PLUGIN_API_VERSION, SceneContext, SceneTopic
from familiar.interfaces.service import CompanionService, ServiceTool, SpeechState
from familiar.interfaces.types import GameEvent

from familiar_desk.feed import utc_now
from familiar_desk.paths import feed_path, lock_path
from familiar_desk.sources.weather import place_for
from familiar_desk.weather import text, weather_settings


class DeskService(CompanionService):
    name = "desk"
    api_version = PLUGIN_API_VERSION
    capabilities = frozenset()

    def __init__(self, settings: dict, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.settings = settings
        self.clock = clock
        self.started = clock()
        sources = settings.get("sources", ["clock"])
        self.weather = weather_settings(settings.get("weather")) if "weather" in sources else None
        place = place_for(settings, self.weather or {})
        self.weather_zone = ZoneInfo(place.timezone) if place.timezone else None
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
        lines = [line for line in (self._clock_line(), self._weather_line()) if line]
        notes = self.settings.get("location_notes")
        if lines and notes:
            lines.append(notes)
        return "\n".join(lines)

    def scene_context(self) -> SceneContext:
        self._read(consume=False)
        clock_data = self._clock_data()
        current, today, _ = self._weather_data()
        condition = (current or today or {}).get("condition")
        frame = [clock_data.get("period", "").replace("_", " ")]
        if condition:
            frame.append(" ".join(text.condition(condition).split()[:2]))
        candidates = (
            ("weather", self._weather_line()),
            ("time", self._clock_line()),
            ("location", self.settings.get("location_notes", "")),
        )
        topics = tuple(SceneTopic(family, family, line) for family, line in candidates if line)
        return SceneContext("; ".join(filter(None, frame)), topics)

    def _entry(self, name: str) -> dict | None:
        entry = self._snapshot["sources"].get(name)
        if entry is None or entry["status"] != "ok":
            return None
        return entry

    def _clock_data(self) -> dict:
        try:
            entry = self._entry("clock")
            if entry is None:
                return {}
            observed = datetime.fromisoformat(entry["observed_at"])
            if (self.clock() - observed).total_seconds() > 3 * self.settings.get("interval_s", 60):
                return {}
            return entry["data"]
        except (ValueError, KeyError, TypeError):
            return {}

    def _clock_line(self) -> str:
        try:
            data = self._clock_data()
            if not data:
                return ""
            local = datetime.fromisoformat(data["local_time"])
            detail = data["period"].replace("_", " ")
            if data.get("sunset"):
                sunset = datetime.fromisoformat(data["sunset"])
                verb = "was" if local >= sunset else "is at"
                detail += f"; sunset {verb} {sunset:%H:%M}"
            where = f" in {data['location']}" if data.get("location") else ""
            return f"Local time{where}: {data['weekday']} {local:%Y-%m-%d %H:%M} ({detail})."
        except (ValueError, KeyError, TypeError):
            return ""

    def _weather_data(self) -> tuple[dict | None, dict | None, str | None]:
        """Uses the observation's own time, so a failing provider never looks fresh."""
        if self.weather is None:
            return None, None, None
        try:
            entry = self._snapshot["sources"].get("weather")
            if entry is None:
                return None, None, None
            data = entry["data"]
            now = self.clock()
            current = self._fresh_current(data.get("current"), now)
            today = data.get("today")
            zone = self.weather_zone
            if today is not None and today["date"] != now.astimezone(zone).date().isoformat():
                today = None
            return current, today, data.get("location")
        except (ValueError, KeyError, TypeError):
            return None, None, None

    def _fresh_current(self, current: dict | None, now: datetime) -> dict | None:
        if current is None:
            return None
        observed = datetime.fromisoformat(current["observed_at"])
        if (now - observed).total_seconds() > self.weather["max_age_s"]:
            return None
        return current

    def _weather_line(self) -> str:
        return text.describe(*self._weather_data())

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
