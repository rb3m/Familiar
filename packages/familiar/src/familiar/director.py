"""Bounded, expiring game-event arbitration on a monotonic clock."""

import asyncio
import logging
import random
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from familiar.crew import Crew, persona_id
from familiar.interfaces import GameAdapter, GameEvent
from familiar.memory import MemoryKeeper

_LOG = logging.getLogger(__name__)

_RANK = {"critical": 0, "notable": 1, "ambient": 2}
_TTL = {"critical": 60, "notable": 90, "ambient": 30}
_IDLE = (
    "Nothing special is happening on the road right now. Make one short, in-character "
    "remark about the surroundings, the cargo or the trip."
)


@dataclass
class _Pending:
    event: GameEvent
    received: float
    deadline: float


def preempts(event: GameEvent) -> bool:
    """Critical events can interrupt a companion's non-critical interjection."""
    return event.priority == "critical"


class Director:
    def __init__(
        self,
        chattiness: float,
        settings: dict,
        clock: Callable[[], float] = time.monotonic,
        *,
        crew: Crew | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.clock = clock
        self.crew = crew
        self.rng = rng or random.Random()
        self.enabled = settings.get("enabled", True)
        scale = 1.5 - chattiness
        self._cooldowns = {
            "notable": settings.get("notable_cooldown_s", 40) * scale,
            "ambient": settings.get("ambient_cooldown_s", 300) * scale,
        }
        self._idle_seconds = settings.get("idle_minutes", 12) * 60 * scale
        self._idle_since = clock()
        self._idle_emitted = False
        self._last_spoke: float | None = None
        self._kinds: dict[str, float] = {}
        self._queue: list[_Pending] = []
        self._busy: set[str] = set()
        self._connected = False
        self._paused = False
        self._changed = asyncio.Event()

    preempts = staticmethod(preempts)

    @staticmethod
    def _key(event: GameEvent) -> tuple:
        if event.kind == "alert":
            return (event.kind, event.data["alert_id"])
        if event.kind == "landmark_nearby":
            return (
                event.kind,
                event.data.get("id")
                or event.data.get("landmark_id")
                or event.data.get("name")
                or event.data.get("landmark_name"),
            )
        return (event.kind,)

    def submit(self, event: GameEvent) -> None:
        _LOG.debug("Game event %s/%s: %s", event.priority, event.kind, event.data.get("summary"))
        ttl = float(event.data.get("ttl_s", _TTL[event.priority]))
        now = self.clock()
        timestamp = event.timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        age = max(0, (datetime.now(UTC) - timestamp).total_seconds())
        if not self.enabled or ttl <= age:
            return
        if event.priority == "ambient" and (not self._connected or self._paused):
            return
        self._queue = [
            item
            for item in self._queue
            if item.deadline > now and self._key(item.event) != self._key(event)
        ]
        self._queue.append(_Pending(event, now, now + ttl - age))
        self._queue.sort(key=lambda item: (_RANK[item.event.priority], item.received))
        del self._queue[8:]
        self._changed.set()

    def set_busy(self, reason: str, busy: bool) -> None:
        if busy:
            self._busy.add(reason)
        if not busy:
            self._busy.discard(reason)
        self._changed.set()

    def set_game_state(self, connected: bool, paused: bool) -> None:
        self._connected, self._paused = connected, paused
        if not connected or paused:
            self._queue = [item for item in self._queue if item.event.priority != "ambient"]
        self._changed.set()

    def notify_spoke(self) -> None:
        self._idle_emitted = False
        self._last_spoke = self._idle_since = self.clock()
        self._queue = [item for item in self._queue if item.event.kind != "idle"]
        self._changed.set()

    def _ready(self, event: GameEvent, now: float) -> bool:
        if not self._connected or self._paused:
            return False
        if preempts(event):
            return not (self._busy - {"speaking"})
        if self._busy:
            return False
        if (
            self._last_spoke is not None
            and now - self._last_spoke < self._cooldowns[event.priority]
        ):
            return False
        return now - self._kinds.get(event.kind, float("-inf")) >= 120

    def _take(self) -> GameEvent | None:
        now = self.clock()
        self._queue = [item for item in self._queue if item.deadline > now]
        if (
            self._connected
            and not self._paused
            and not self._queue
            and not self._idle_emitted
            and now - self._idle_since >= self._idle_seconds
            and now - self._kinds.get("idle", float("-inf")) >= 120
        ):
            self.submit(GameEvent("idle", "ambient", {"summary": _IDLE}, datetime.now(UTC)))
        for index, item in enumerate(self._queue):
            if self._ready(item.event, now):
                self._queue.pop(index)
                if item.event.kind == "idle":
                    self._idle_emitted = True
                self._kinds[item.event.kind] = now
                return self._assign(item.event)
        return None

    def _assign(self, event: GameEvent) -> GameEvent:
        if self.crew is None:
            return event
        speaker = self.crew.pick_for_event(event)
        if event.kind == "idle" and event.priority != "critical":
            speaker = self.crew.pick_idle(self.rng)
        return replace(event, data={**event.data, "speaker_id": persona_id(speaker)})

    async def next(self) -> GameEvent:
        while True:
            self._changed.clear()
            if self.enabled:
                event = self._take()
                if event is not None:
                    return event
            with suppress(TimeoutError):
                async with asyncio.timeout(0.1):
                    await self._changed.wait()


async def feed_events(
    adapter: GameAdapter, director: Director, memory: MemoryKeeper | None = None
) -> None:
    """Refresh state before admitting each event, including log-only updates."""
    snapshot = await adapter.snapshot()
    director.set_game_state(snapshot["connected"], snapshot["paused"])
    async for event in adapter.events():
        snapshot = await adapter.snapshot()
        director.set_game_state(snapshot["connected"], snapshot["paused"])
        if memory is not None:
            memory.observe(event)
        director.submit(event)
        if memory is None:
            continue
        recall = memory.maybe_recall(event)
        if recall is not None:
            director.submit(recall)
