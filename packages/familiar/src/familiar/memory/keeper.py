"""Observe trip facts and retrieve small, explicitly historical memory blocks."""

import functools
import logging
import random
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from familiar.interfaces import GameEvent, LLMProvider
from familiar.transcript import TranscriptLog

from .store import MemoryStore
from .summary import summarize
from .text import compact, date_label, words

RECALL_MIN_SALIENCE = 0.5
RECALL_EPISODE_COOLDOWN = timedelta(days=7)
RECALL_PROBABILITY = 0.25
RECALL_COOLDOWN_MIN = 45
RECALL_TTL_S = 120
CONTEXT_EPISODE_LIMIT = 3
MEMORY_HEADER = (
    "Memories from earlier trips (past, not happening now; use only if relevant or asked):"
)
CREW_CALLBACK_PROBABILITY = 0.25
CREW_CALLBACK_MIN_AGE = timedelta(hours=12)
CREW_MOMENT_TEXT_LIMIT = 300
CREW_REPLY_WINDOW = timedelta(seconds=30)
CREW_HEADER = "Crew memories — crew said (not fact); past banter, never game facts:"
RECALL_CUES = ("have we", "remember", "last time", "before", "earlier", "previous", "been here")


_LOG = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _never_fatal(default):
    """Memory is optional: a database error must not stop game events or a reply."""

    def wrap(method):
        @functools.wraps(method)
        def guarded(*args, **kwargs):
            try:
                return method(*args, **kwargs)
            except sqlite3.Error:
                _LOG.warning("Trip memory unavailable for %s", method.__name__, exc_info=True)
                return default

        return guarded

    return wrap


class MemoryKeeper:
    def __init__(
        self,
        store: MemoryStore,
        game: str,
        persona: str,
        whereabouts: Callable[[], dict | None] | None = None,
        clock: Callable[[], datetime] = _now,
        rng: random.Random | None = None,
        settings: dict | None = None,
        crew: tuple[str, ...] | None = None,
    ) -> None:
        self.store = store
        self.game = game
        self.persona = persona
        self.crew = crew or (persona,)
        self.whereabouts = whereabouts
        self.clock = clock
        self.rng = rng if rng is not None else random.Random()
        settings = settings or {}
        self.probability = settings.get("recall_probability", RECALL_PROBABILITY)
        self.cooldown = timedelta(minutes=settings.get("recall_cooldown_min", RECALL_COOLDOWN_MIN))
        self.session_id = store.start_session(game, persona, clock())
        self._terms: set[str] = set()
        self._terms_revision = -1
        self._finished = False

    def _location(self) -> dict | None:
        if self.whereabouts is None:
            return None
        location = self.whereabouts()
        if not location or not location.get("place_key"):
            return None
        return location

    @_never_fatal(None)
    def observe(self, event: GameEvent) -> None:
        if event.kind in {"banter", "crew_moment"}:
            return
        salience = event.data.get("memory_salience", 0)
        if salience <= 0:
            return
        location = self._location()
        if location is None:
            return
        self.store.add_episode(
            self.session_id,
            self.game,
            event.kind,
            location["place_key"],
            location.get("place", ""),
            location.get("region", ""),
            location.get("activity", ""),
            event.data["summary"],
            salience,
            self.clock(),
            speaker=event.data.get("speaker_id"),
            source="game",
        )

    @_never_fatal(None)
    def remember_crew(self, lines: list[tuple[str, str]]) -> int | None:
        if not lines:
            return None
        location = self._location() or {}
        speakers = ", ".join(dict.fromkeys(speaker for speaker, _ in lines))
        text = "\n".join(f"{speaker}: {text}" for speaker, text in lines)
        return self.store.add_episode(
            self.session_id,
            self.game,
            "crew_moment",
            location.get("place_key", ""),
            location.get("place", ""),
            location.get("region", ""),
            location.get("activity", ""),
            text[:CREW_MOMENT_TEXT_LIMIT],
            0,
            self.clock(),
            speaker=speakers,
            source="persona_claim",
        )

    def _place_rank(self, episode: dict, location: dict) -> int:
        if location.get("place_key") and episode["place_key"] == location["place_key"]:
            return 2
        if location.get("region") and episode["region"] == location["region"]:
            return 1
        return 0

    def _crew_text(self, episode: dict) -> str:
        return (
            f"on {date_label(episode['ts_utc'], self.clock())}, "
            f"{episode['speaker']} said: {episode['text']}. This was banter, not fact."
        )

    def _callback_ready(self, episode: dict) -> bool:
        if self.clock() - datetime.fromisoformat(episode["ts_utc"]) <= CREW_CALLBACK_MIN_AGE:
            return False
        last = self.store.moment_last_use(episode["id"])
        return last is None or self.clock() - last >= RECALL_EPISODE_COOLDOWN

    @_never_fatal("")
    def crew_callback(self) -> str:
        """Charge on inclusion, even if the generated scene ignores the optional hint."""
        last = self.store.latest_recall()
        if last is not None and self.clock() - last < self.cooldown:
            return ""
        candidates = [
            row for row in self.store.crew_moments(self.game) if self._callback_ready(row)
        ]
        if not candidates or self.rng.random() >= CREW_CALLBACK_PROBABILITY:
            return ""
        location = self._location() or {}
        episode = max(candidates, key=lambda row: self._place_rank(row, location))
        self.store.record_recall(episode["id"], self.persona, self.clock())
        return "\nOptional callback, use only if it fits naturally: " + self._crew_text(episode)

    def _crew_context(self, user_text: str) -> str:
        query = set(words(user_text).split())
        location = self._location() or {}
        candidates = self.store.crew_moments(self.game)
        ranked = sorted(
            candidates,
            key=lambda row: (
                len(query & set(words(row["text"]).split())),
                self._place_rank(row, location),
            ),
            reverse=True,
        )
        if not ranked:
            return ""
        return (
            CREW_HEADER
            + "\n"
            + "\n".join("- " + self._crew_text(row) for row in ranked[:CONTEXT_EPISODE_LIMIT])
        )

    def _episode_text(self, episode: dict, limit: int = 39) -> str:
        location = ", ".join(part for part in (episode["place"], episode["region"]) if part)
        return compact(
            f"{date_label(episode['ts_utc'], self.clock())}: near {location}, "
            f"{episode['activity']}: {episode['text']}",
            limit,
        )

    @_never_fatal(None)
    def maybe_recall(self, event: GameEvent) -> GameEvent | None:
        if event.data.get("memory_arrival") is not True:
            return None
        location = self._location()
        if location is None:
            return None
        candidates = self.store.episodes_at_place(
            self.game,
            location["place_key"],
            self.session_id,
            limit=1,
            min_salience=RECALL_MIN_SALIENCE,
        )
        if not candidates:
            return None
        episode = candidates[0]
        if not self.recall_ready(episode["id"]):
            return None
        if self.rng.random() >= self.probability:
            return None
        dated, details = self._episode_text(episode, 80).split(": ", 1)
        return GameEvent(
            "memory_recall",
            "ambient",
            {
                "summary": f"Memory from {dated}, not happening now: {details}",
                "memory_episode_id": episode["id"],
                "ttl_s": RECALL_TTL_S,
            },
            self.clock(),
        )

    @_never_fatal(False)
    def recall_ready(self, episode_id: int) -> bool:
        """Also checked at speech admission: another recall may have spoken while queued."""
        now = self.clock()
        last = self.store.latest_recall()
        if last is not None and now - last < self.cooldown:
            return False
        last = self.store.crew_last_recall(episode_id, self.crew)
        return last is None or now - last >= RECALL_EPISODE_COOLDOWN

    @_never_fatal(None)
    def recalled(self, event: GameEvent, speaker: str | None = None) -> None:
        """Called only after a memory interjection has been voiced/consumed."""
        self.store.record_recall(
            event.data["memory_episode_id"], speaker or self.persona, self.clock()
        )

    def _matches(self, user_text: str) -> list[dict]:
        if self._terms_revision != self.store.revision:
            self._terms = self.store.known_terms(self.game)
            self._terms_revision = self.store.revision
        query = f" {words(user_text)} "
        terms = sorted(term for term in self._terms if f" {term} " in query)
        return self.store.search_terms(self.game, terms, self.session_id)

    def _local_context(self) -> tuple[list[dict], list[str]]:
        location = self._location()
        if location is None:
            return [], []
        local = self.store.episodes_at_place(self.game, location["place_key"], self.session_id)
        if local:
            return local, []
        regional = self.store.region_history(self.game, location.get("region", ""), self.session_id)
        return regional, [self._visit_note(location, regional)]

    @_never_fatal("")
    def context(self, user_text: str) -> str:
        local, notes = self._local_context()
        matches = self._matches(user_text)
        # Keep one local fact, then named matches, then fill remaining slots locally.
        ordered = local[:1] + matches + local[1:]
        unique = {}
        for episode in ordered:
            unique.setdefault(episode["id"], episode)
        selected = list(unique.values())[:CONTEXT_EPISODE_LIMIT]
        notes.extend(self._episode_text(episode) for episode in selected)
        query = f" {words(user_text)} "
        crew_context = ""
        if any(f" {cue} " in query for cue in RECALL_CUES):
            crew_context = self._crew_context(user_text)
            notes.extend(
                compact(
                    f"Trip summary {date_label(row['started_utc'], self.clock())}: {row['summary']}"
                )
                for row in self.store.latest_summaries(self.game, self.session_id)
            )
        blocks = [crew_context]
        if notes:
            blocks.insert(0, MEMORY_HEADER + "\n" + "\n".join("- " + note for note in notes))
        return "\n\n".join(filter(None, blocks))

    def _visit_note(self, location: dict, regional: list[dict]) -> str:
        place = location.get("place", location["place_key"])
        if not regional:
            return f"No record of earlier visits near {place}."
        date = date_label(regional[0]["ts_utc"], self.clock())
        region = location.get("region", "")
        return f"First recorded time near {place}; we were in {region} on {date}."

    async def finish(self, llm: LLMProvider, transcript: TranscriptLog) -> None:
        if self._finished:
            return
        self._finished = True
        summary = ""
        try:
            summary = await summarize(llm, transcript)
        finally:
            self._end_session(summary)

    @_never_fatal(None)
    def _end_session(self, summary: str) -> None:
        try:
            self.store.end_session(self.session_id, summary, self.clock())
        finally:
            self.store.close()
