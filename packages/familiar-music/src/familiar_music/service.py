"""Music companion service: spoken tools, rare track remarks and speech coordination."""

import asyncio
import logging
import random
import time
from contextlib import suppress
from datetime import UTC, datetime
from functools import partial
from threading import Event

from familiar.interfaces import PLUGIN_API_VERSION, GameEvent, ServiceTool, ToolSpec, spoken

from .dj import DJ, Dislikes
from .library.enrich import Enricher
from .library.index import LibraryIndex
from .library.paths import database_path, library_roots
from .library.scanner import Scanner
from .mpv import MpvPlayer
from .pear import PearPlayer
from .search import Searcher
from .settings import music_settings, pear_settings
from .sourced import SourcedPlayer

_LOG = logging.getLogger(__name__)

# Requests that choose what plays: they reset the DJ-turn count and start the autonomy hold.
SELECTION_TOOLS = frozenset(
    {"play_music", "skip_track", "more_like_this", "calmer_music", "dislike_track"}
)
# Pausing stops autonomous picks until music is resumed or chosen again.
PICKS_PAUSED = {"pause_music": True, "resume_music": False, "play_music": False}
# Tools an unprompted DJ turn may use.
AUTONOMOUS_TOOLS = frozenset({"play_music"})
DJ_TURN = (
    "Music check: now playing {track}. You pick the crew's music. If something else would suit "
    "this moment or your own taste better, call play_music (kind genre with a descriptive "
    "playlist search, or an artist) and say one short line about your pick. Otherwise keep it "
    "and make at most a brief remark about the song."
)


class Cadence:
    """Due after a random number of song changes in [low, high]; restarts only once heard."""

    def __init__(self, bounds, rng):
        self.bounds, self.rng = bounds, rng
        self.reset()

    def reset(self):
        self.count = 0
        self.due = self.rng.randint(*self.bounds)

    def tick(self):
        self.count += 1

    @property
    def ready(self):
        return self.count >= self.due


class MusicService:
    name = "pear"
    api_version = PLUGIN_API_VERSION
    capabilities = frozenset({"tools", "events", "speech_state"})

    def __init__(
        self,
        settings,
        *,
        player=None,
        searcher=None,
        index=None,
        clock=time.monotonic,
        sleeper=asyncio.sleep,
        rng=None,
    ):
        self.settings = music_settings(settings)
        self.index = index
        self.vocabulary = []
        self._library_task = None
        self._library_started = False
        self._library_stop = Event()
        if index is not None:
            self.vocabulary = [row["tag"] for row in index.tag_vocabulary(limit=40)]
        self.player = player or self._build_player(settings)
        rng = rng or random.Random()
        self.dj = DJ(
            self.player,
            searcher or Searcher(timeout=self.settings["search_timeout_s"]),
            settings=self.settings,
            dislikes=Dislikes(settings.get("dislike_read"), settings.get("dislike_write")),
            clock=clock,
            rng=rng,
            index=index,
            taste_read=settings.get("taste_read"),
        )
        self.clock, self.sleep = clock, sleeper
        self.poll_interval = pear_settings(settings)["poll_interval_s"]
        self.recovery_interval = pear_settings(settings)["recovery_interval_s"]
        self._current = None
        self._last_uri = None
        self._observed_track = None
        self._observed_at = None
        self._listened_s = 0.0
        self._play_interrupted = False
        self._play_cause = None
        self._play_tasks = set()
        rng = rng or random.Random()
        self.remarks = Cadence(self.settings["remark_every"], rng)
        self.picks = Cadence(self.settings["dj_turn_every"], rng)
        self._hold_until = float("-inf")
        self._picks_paused = False
        self._turn_valid = False  # the last DJ turn emitted is still the Commander's wish
        self._speaking_kind = None  # our event kind being spoken right now, if any
        self._uncounted = False  # the next song change came from a request
        self._hint_logged = False
        self._tools_lock = asyncio.Lock()
        self._available = False
        self._recovered = asyncio.Event()

    def _build_player(self, settings):
        if self.index is None:
            return PearPlayer(settings)
        return SourcedPlayer(
            {
                "ytm": PearPlayer(settings),
                "local": MpvPlayer(settings, resolver=self.index.resolve),
            }
        )

    async def _refresh_library(self):
        await asyncio.to_thread(self._scan_and_enrich)

    def _scan_and_enrich(self):
        try:
            Scanner(self.index).scan(library_roots(self.settings))
            if not self.settings["library_enrich"] or self._library_stop.is_set():
                return
            self._enrich_library()
        except InterruptedError:
            return
        except Exception:
            _LOG.warning("Music library refresh failed", exc_info=True)

    def _enrich_library(self):
        enricher = Enricher(self.index)
        try:
            enricher.enrich_all(progress_cb=self._enrich_progress)
        finally:
            enricher.client.close()

    def _enrich_progress(self, *_):
        # The worker owns its client until the in-flight lookup finishes.
        if self._library_stop.is_set():
            raise InterruptedError("Music library enrichment stopped")

    def _track_facts(self):
        if self._current is None:
            return {}
        facts = {"album": self._current.album, "year": self._current.year}
        return {key: value for key, value in facts.items() if value}

    def _remark_summary(self):
        summary = self._current.summary()
        if not self._current.uri.startswith("local:"):
            return summary
        if self._current.album:
            summary += f", from {self._current.album}"
        if self._current.year is not None:
            summary += f" ({self._current.year})"
        return summary

    def describe(self):
        if self._current is None:
            return ""
        state = {True: "Paused", False: "Now playing"}[self._current.paused]
        return f"{state}: {self._current.summary()}."

    async def _health_error(self):
        health = await self.player.health()
        self._available = health.status == "ok"
        if self._available:
            self._recovered.set()
            return None
        self._recovered.clear()
        self._current = None
        if not self._hint_logged:
            _LOG.warning("Music unavailable: %s", health.hint)
            self._hint_logged = True
        return {"error": health.hint}

    async def _execute(self, name, **arguments):
        async with self._tools_lock:
            try:
                error = await self._health_error()
                if error:
                    return error
                if self._overruled(name):
                    return {"error": "The Commander just chose the music; keep it this time"}
                await self._observe_player()
                self._note_request(name)
                result = await self._dispatch(name, arguments)
                await self._observe_player(name=name, result=result)
                self._uncounted |= name in SELECTION_TOOLS and "error" not in result
                return result
            except Exception as exc:
                _LOG.debug("Music tool failed", exc_info=True)
                return {"error": f"Music unavailable: {str(exc) or type(exc).__name__}"}

    def _tool_cause(self, name, result, current):
        if name not in SELECTION_TOOLS or "error" in result:
            return None
        explicit = {"skip_track": "skipped", "dislike_track": "disliked"}
        if name in explicit:
            return explicit[name]
        if current != self._observed_track or "playing" in result:
            return "ignored"
        return None

    async def _observe_player(self, *, name=None, result=None):
        """Signal collection must never turn a working control into a failed tool."""
        try:
            current = await self.player.now_playing()
            cause = self._tool_cause(name, result or {}, current)
            self._observe_play(current, cause=cause)
        except Exception:
            _LOG.debug("Music behaviour observation failed", exc_info=True)

    def _overruled(self, name):
        """A DJ turn queued before a request or pause must not undo it once it gets to speak."""
        dj_turn = self._speaking_kind == "music_dj_turn"
        return dj_turn and not self._turn_valid and name in SELECTION_TOOLS

    def _note_request(self, name):
        """Whoever chose the music just now, the DJ waits before choosing again."""
        self._picks_paused = PICKS_PAUSED.get(name, self._picks_paused)
        if name not in SELECTION_TOOLS and name not in PICKS_PAUSED:
            return
        self._turn_valid = False
        if name not in SELECTION_TOOLS:
            return
        self._hold_until = self.clock() + self.settings["autonomy_hold_s"]
        self.picks.reset()

    async def _dispatch(self, name, arguments):
        handlers = {
            "play_music": self.dj.seed,
            "skip_track": self.dj.skip,
            "more_like_this": self.dj.reseed,
            "calmer_music": partial(self.dj.reseed, calmer=True),
            "dislike_track": self.dj.dislike,
            "set_music_volume": self.dj.volume,
            "pause_music": self.dj.pause,
            "resume_music": self.dj.resume,
        }
        if name in handlers:
            return await handlers[name](**arguments)
        if name == "now_playing":
            self._current = await self.player.now_playing()
            return {
                "playing": self.describe() or "Nothing is playing",
                **self._track_facts(),
                "duration": spoken(
                    self._current.duration_s if self._current is not None else None, "s"
                ),
            }
        if "like" not in self.player.capabilities:
            return {"error": "This player does not support likes"}
        await self.player.like()
        return {"status": "Liked track"}

    def _vocabulary_description(self):
        if not self.vocabulary:
            return ""
        return " Library tags for genres: " + ", ".join(self.vocabulary) + "."

    def service_tools(self):
        string = {"type": "string"}
        definitions = {
            "play_music": (
                "Play music. kind=genre for a genre, era or mood: write the query as a "
                "descriptive playlist search such as '70s disco classics' or 'late night jazz "
                "bar'; it queues several songs. kind=artist or song plays that, then similar "
                "music." + self._vocabulary_description(),
                {
                    "query": string,
                    "mood": string,
                    "genres": {"type": "array", "items": string},
                    "years": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    "source": {"type": "string", "enum": ["library", "youtube", "any"]},
                    "kind": {"type": "string", "enum": ["genre", "artist", "song"]},
                },
                ["query", "kind"],
            ),
            "skip_track": ("Skip the current track", {}, []),
            "dislike_track": ("Remember a dislike and skip", {}, []),
            "like_track": ("Like the current track", {}, []),
            "more_like_this": ("Queue similar music", {}, []),
            "calmer_music": ("Choose calmer music", {}, []),
            "set_music_volume": (
                "Set volume or change it, specifying exactly one",
                {
                    "level": {"type": "number", "minimum": 0, "maximum": 100},
                    "change": {"type": "number", "minimum": -100, "maximum": 100},
                },
                [],
            ),
            "pause_music": ("Pause music", {}, []),
            "resume_music": ("Resume music", {}, []),
            "now_playing": ("Identify the current music", {}, []),
        }
        return [
            ServiceTool(
                ToolSpec(
                    name,
                    description,
                    {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                        "additionalProperties": False,
                    },
                ),
                partial(self._execute, name),
                name != "now_playing",
                name in AUTONOMOUS_TOOLS,
            )
            for name, (description, properties, required) in definitions.items()
        ]

    async def poll(self):
        """One poll, exposed for deterministic tests and alternative schedulers."""
        async with self._tools_lock:
            return await self._poll()

    async def _poll(self):
        self._current = await self.dj.current_track()
        self._observe_play(self._current)
        if self._current is None:
            await self.dj.top_up()
            self._current = await self.dj.current_track()
            self._observe_play(self._current)
        self._available = self._current is not None
        if self._current is None:
            return None
        uri = self._current.uri
        if uri == self._last_uri:
            return None
        self._last_uri = uri
        await self.dj.top_up()
        uncounted = uri in self.dj.announced or self._uncounted
        self.dj.announced.discard(uri)
        self._uncounted = False
        if uncounted:
            # A request caused it (and the reply named it); it is not a natural change to count.
            return None
        self.remarks.tick()
        self.picks.tick()
        return self._dj_turn() or self._remark()

    def _observe_play(self, current, *, cause=None):
        """Poll time, excluding pauses; changing source never loses the previous identity."""
        now = self.clock()
        previous = self._observed_track
        if previous is not None and not previous.paused:
            self._listened_s += max(0, now - self._observed_at)
        changed = previous is not None and (current is None or current.uri != previous.uri)
        if changed:
            self._finish_play(previous, cause or self._play_cause, ended=current is None)
        if previous is None or changed:
            self._listened_s = 0.0
            self._play_interrupted = False
            self._play_cause = None
        # A player can acknowledge a skip/play command on a later poll.
        if not changed and cause is not None and previous is not None:
            self._play_cause = cause
        self._observed_track, self._observed_at = current, now

    def _finish_play(self, track, cause, *, ended):
        if cause == "ignored" or self._play_interrupted:
            return
        listened = self._listened_s
        duration = track.duration_s
        if duration is not None:
            listened = min(listened, duration)
        outcome = cause
        # ADR 0015: only an early skip is negative; a later one is judged like a natural change.
        if outcome == "skipped" and listened >= self.settings["skip_early_s"]:
            outcome = None
        # A dislike is always negative, however long it played; storage knows only skips.
        outcome = {"disliked": "skipped"}.get(outcome, outcome)
        if outcome is None:
            outcome = self._natural_outcome(duration, listened, ended)
        if outcome is None:
            return
        task = asyncio.create_task(self._record_play(track, outcome, listened))
        self._play_tasks.add(task)
        task.add_done_callback(self._play_tasks.discard)

    def _natural_outcome(self, duration, listened, ended):
        if duration and listened >= duration:
            return "played"
        if listened < self.settings["skip_early_s"]:
            return "skipped"
        if duration and listened >= duration * self.settings["played_fraction"]:
            return "played"
        if ended and duration is None:
            return "played"
        return None

    async def _record_play(self, track, outcome, listened):
        at = datetime.now(UTC).isoformat()
        try:
            await asyncio.to_thread(self._write_play, track, at, outcome, listened)
        except Exception:
            _LOG.debug("Music behaviour write failed", exc_info=True)

    def _write_play(self, track, at, outcome, listened):
        index = self.index
        if index is None:
            index = LibraryIndex(database_path(self.settings))
        index.record_play(track, at, outcome, listened)

    def _may_pick(self):
        return (
            self.settings["autonomous_picks"]
            and not self._picks_paused
            and not self._current.paused
            and self.clock() >= self._hold_until
            and self.picks.ready
        )

    def _dj_turn(self):
        if not self._may_pick():
            return None
        self._turn_valid = True
        return self._cue(
            "music_dj_turn",
            DJ_TURN.format(track=self._current.summary()),
            ttl_s=20,
            speaker="carrier",
            tools=sorted(AUTONOMOUS_TOOLS),
            taste="music",
        )

    def _remark(self):
        if not self.remarks.ready:
            return None
        return self._cue("music_track", f"Now playing: {self._remark_summary()}.", ttl_s=15)

    def _cue(self, kind, summary, **data):
        """A moment-bound event: it waits for silence but skips the Director's quiet gaps."""
        return GameEvent(
            kind,
            "ambient",
            {
                "summary": summary,
                "uri": self._current.uri,
                "companion_service": True,
                "cue": True,
                **data,
            },
            datetime.now(UTC),
        )

    async def _wait_recovery(self):
        """Wait for a tool to recover the player, re-checking health quietly meanwhile."""
        while not self._recovered.is_set():
            with suppress(TimeoutError):
                await asyncio.wait_for(self._recovered.wait(), self.recovery_interval)
                return
            await self._health_error()

    async def events(self):
        if self.index is not None and not self._library_started:
            self._library_started = True
            self._library_task = asyncio.create_task(self._refresh_library())
        # A failure parks polling until a tool succeeds or a periodic health check passes.
        if await self._health_error():
            await self._wait_recovery()
        while True:
            try:
                event = await self.poll()
                if event is not None:
                    yield event
            except Exception:
                self._available = False
                self._current = None
                self._recovered.clear()
                _LOG.debug("Music polling parked", exc_info=True)
                await self._wait_recovery()
            await self.sleep(self.poll_interval)

    def _acknowledge(self, state):
        """The Director let our event through: restart the counts it was due for."""
        heard = {"music_track": (self.remarks,), "music_dj_turn": (self.remarks, self.picks)}
        kind = state.event_kind if state.speaking and state.event_kind in heard else None
        started = kind is not None and kind != self._speaking_kind
        self._speaking_kind = kind
        # Voice repeats states within one turn (e.g. push-to-talk); count each turn once.
        for cadence in heard[kind] if started else ():
            cadence.reset()

    async def speech_state(self, state):
        self._acknowledge(state)
        if not self._available and not self.dj.interrupting:
            return
        try:
            self._observe_play(await self.player.now_playing())
            await self.dj.speech_state(state)
            self._play_interrupted |= self.dj.paused_for_speech
            self._observe_play(await self.player.now_playing())
        except Exception:
            self._available = False
            raise

    async def aclose(self):
        self._library_stop.set()
        if self._play_tasks:
            await asyncio.gather(*self._play_tasks)
        if self._library_task is not None:
            self._library_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._library_task
        try:
            await self.dj.aclose()
        finally:
            await self.player.aclose()


# Keep existing integrations and the pear entry point YouTube-only.
PearMusicService = MusicService


class BlendMusicService(MusicService):
    """Local library (mpv) blended with YouTube Music; the `blend` entry point."""

    name = "blend"

    def __init__(self, settings, **kwargs):
        kwargs.setdefault("index", _library_index(settings))
        super().__init__(settings, **kwargs)


def _library_index(settings):
    try:
        return LibraryIndex(database_path(settings))
    except Exception:
        _LOG.warning("Music library unavailable; using YouTube Music", exc_info=True)
        return None


# Kept for scripts written against the earlier factory.
build_blend = BlendMusicService
