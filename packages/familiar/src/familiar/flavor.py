"""Gate optional quote candidates supplied by game packs."""

import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from familiar.interfaces import GameEvent
from familiar.memory.keeper import _never_fatal
from familiar.memory.store import MemoryStore

FLAVOR_PROBABILITY = 0.2
FLAVOR_COOLDOWN_MIN = 30
FLAVOR_ITEM_COOLDOWN_DAYS = 14


def _now() -> datetime:
    return datetime.now(UTC)


class FlavorKeeper:
    def __init__(
        self,
        store: MemoryStore | None = None,
        game: str = "",
        clock: Callable[[], datetime] = _now,
        rng: random.Random | None = None,
        settings: dict | None = None,
    ) -> None:
        self.store = store
        self.game = game
        self.clock = clock
        self.rng = rng if rng is not None else random.Random()
        settings = settings or {}
        self.probability = settings.get("probability", FLAVOR_PROBABILITY)
        self.cooldown = timedelta(minutes=settings.get("cooldown_min", FLAVOR_COOLDOWN_MIN))
        self.item_cooldown = timedelta(
            days=settings.get("item_cooldown_days", FLAVOR_ITEM_COOLDOWN_DAYS)
        )
        self._latest: datetime | None = None
        self._uses: dict[tuple[str, str], datetime] = {}

    @_never_fatal(None)
    def choose(self, event: GameEvent) -> dict | None:
        if event.priority == "critical" or not event.data.get("flavor"):
            return None
        now = self.clock()
        latest = self._latest
        if self.store is not None:
            latest = max(
                (value for value in (self.store.latest_flavor_use(), latest) if value is not None),
                default=None,
            )
        if latest is not None and now - latest < self.cooldown:
            return None
        if self.rng.random() >= self.probability:
            return None
        candidates = [
            candidate
            for candidate in event.data["flavor"]
            if self._item_ready(candidate["id"], now)
        ]
        if not candidates:
            return None
        return self.rng.choice(candidates)

    def _item_ready(self, item_id: str, now: datetime) -> bool:
        last = self._uses.get((self.game, item_id))
        if self.store is not None:
            last = max(
                (
                    value
                    for value in (self.store.last_flavor_use(self.game, item_id), last)
                    if value is not None
                ),
                default=None,
            )
        return last is None or now - last >= self.item_cooldown

    @_never_fatal(None)
    def used(self, game: str, candidate: dict) -> None:
        """Record only after the first sentence was spoken/consumed."""
        now = self.clock()
        self._latest = now
        self._uses[game, candidate["id"]] = now
        if self.store is not None:
            self.store.record_flavor_use(game, candidate["id"], now)


def flavor_hint(candidate: dict) -> str:
    credit = f"crediting {candidate['speaker']} naturally"
    if candidate["attribution"] == "attributed":
        credit = (
            "hedging the attribution with "
            f"'as the line often credited to {candidate['speaker']} goes'"
        )
    return (
        "\nYou MAY loosely paraphrase this real quote in your own voice, "
        f"{credit}, only if it fits; never quote it at length: "
        f"{candidate['text']} (context: {candidate['context']})."
    )
