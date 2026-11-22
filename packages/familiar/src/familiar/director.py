"""Bounded, expiring game-event arbitration on a monotonic clock."""

import asyncio
import logging
import random
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from familiar.banter import TUNING, Budget
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
        banter: int = 0,
        banter_blocked: Callable[[], str | None] | None = None,
        desk: bool = False,
    ) -> None:
        self.clock = clock
        self.crew = crew
        self.rng = rng or random.Random()
        self.banter = None
        if crew is not None and len(crew.members) > 1 and banter > 0:
            self.banter = Budget(banter, crew)
        self.banter_blocked = banter_blocked or (lambda: None)
        self._banter_retry = clock()
        self._last_banter_skip: str | None = None
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
        self._connected = desk
        self._paused = False
        self._changed = asyncio.Event()

    preempts = staticmethod(preempts)

    @staticmethod
    def _key(event: GameEvent) -> tuple:
        if event.kind == "research_result":
            return (event.kind, event.data["research_id"])
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
        if self.banter is not None and event.kind in {"idle", "banter"}:
            self._reset_quiet()
            _LOG.debug("Dropping idle/banter item: scene admission changed")
            return
        ttl = float(event.data.get("ttl_s", _TTL[event.priority]))
        now = self.clock()
        timestamp = event.timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        age = max(0, (datetime.now(UTC) - timestamp).total_seconds())
        if not self.enabled or ttl <= age:
            return
        if (
            not event.data.get("companion_service")
            and event.priority == "ambient"
            and (not self._connected or self._paused)
        ):
            return
        self._queue = [
            item
            for item in self._queue
            if item.deadline > now and self._key(item.event) != self._key(event)
        ]
        self._reset_quiet()
        self._queue.append(_Pending(event, now, now + ttl - age))
        self._queue.sort(key=lambda item: (_RANK[item.event.priority], item.received))
        del self._queue[8:]
        self._changed.set()

    def set_busy(self, reason: str, busy: bool) -> None:
        if busy:
            self._busy.add(reason)
        if not busy:
            self._busy.discard(reason)
        if busy or reason in {"user", "speaking", "reply", "pending"}:
            self._reset_quiet()
        self._changed.set()

    def _reset_quiet(self) -> None:
        if self.banter is not None:
            self._idle_since = self.clock()

    def set_game_state(self, connected: bool, paused: bool) -> None:
        self._connected, self._paused = connected, paused
        if not connected or paused:
            self._queue = [
                item
                for item in self._queue
                if item.event.priority != "ambient" or item.event.data.get("companion_service")
            ]
        self._changed.set()

    def notify_spoke(self) -> None:
        self._idle_emitted = False
        self._last_spoke = self._idle_since = self.clock()
        self._queue = [item for item in self._queue if item.event.kind not in {"idle", "banter"}]
        self._changed.set()

    def _ready(self, event: GameEvent, now: float) -> bool:
        if not event.data.get("companion_service") and (not self._connected or self._paused):
            return False
        if preempts(event):
            return not (self._busy - {"speaking", "pending"})
        if self._busy:
            return False
        if event.data.get("cue"):
            # Tied to a moment (e.g. a song change): wait only for silence, not quiet gaps.
            return True
        if (
            self._last_spoke is not None
            and now - self._last_spoke < self._cooldowns[event.priority]
        ):
            return False
        return now - self._kinds.get(event.kind, float("-inf")) >= 120

    def _take(self) -> GameEvent | None:
        now = self.clock()
        if self.banter is not None and self._queue:
            last_queued = max(min(now, item.deadline) for item in self._queue)
            self._idle_since = max(self._idle_since, last_queued)
        self._queue = [item for item in self._queue if item.deadline > now]
        scene = self._take_banter(now)
        if scene is not None:
            return scene
        if (
            self.banter is None
            and self._connected
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

    def _take_banter(self, now: float) -> GameEvent | None:
        if self.banter is None:
            return None
        reason = self._banter_skip(now)
        if reason:
            self._log_banter_skip(reason)
            return None
        self._last_banter_skip = None
        self._banter_retry = now + TUNING["retry_s"]
        return GameEvent("banter", "ambient", {"summary": "Crew banter"}, datetime.now(UTC))

    def _log_banter_skip(self, reason: str) -> None:
        if reason == self._last_banter_skip:
            return
        self._last_banter_skip = reason
        _LOG.debug("Skipping banter: %s", reason)

    def _banter_skip(self, now: float) -> str | None:
        assert self.banter is not None
        if not self._connected or self._paused:
            return "game paused or disconnected"
        if self._queue:
            self._idle_since = now
            return "queued event"
        if self._busy:
            self._idle_since = now
            return "push-to-talk, speech or reply active"
        if now < self._banter_retry or now - self._idle_since < self.banter.gap:
            return "quiet gap"
        if not self.banter.available(now):
            return "rolling-hour cap"
        return self.banter_blocked()

    def banter_started(self) -> None:
        """Charge only validated scenes, immediately before playback starts."""
        if self.banter is not None:
            self.banter.consume(self.clock())

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
