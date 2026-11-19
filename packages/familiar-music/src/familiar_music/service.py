"""Pear companion service: spoken tools, rare track remarks and speech coordination."""

import asyncio
import logging
import time
from contextlib import suppress
from datetime import UTC, datetime
from functools import partial

from familiar.interfaces import PLUGIN_API_VERSION, GameEvent, ServiceTool, ToolSpec, spoken

from .dj import DJ, Dislikes
from .pear import PearPlayer
from .search import Searcher
from .settings import music_settings, pear_settings

_LOG = logging.getLogger(__name__)


class PearMusicService:
    name = "pear"
    api_version = PLUGIN_API_VERSION
    capabilities = frozenset({"tools", "events", "speech_state"})

    def __init__(
        self, settings, *, player=None, searcher=None, clock=time.monotonic, sleeper=asyncio.sleep
    ):
        self.settings = music_settings(settings)
        self.player = player or PearPlayer(settings)
        self.dj = DJ(
            self.player,
            searcher or Searcher(timeout=self.settings["search_timeout_s"]),
            settings=self.settings,
            dislikes=Dislikes(settings.get("dislike_read"), settings.get("dislike_write")),
            clock=clock,
        )
        self.clock, self.sleep = clock, sleeper
        self.poll_interval = pear_settings(settings)["poll_interval_s"]
        self.recovery_interval = pear_settings(settings)["recovery_interval_s"]
        self._current = None
        self._last_id = None
        self._last_remark = float("-inf")
        self._hint_logged = False
        self._tools_lock = asyncio.Lock()
        self._available = False
        self._recovered = asyncio.Event()

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
                return await self._dispatch(name, arguments)
            except Exception as exc:
                _LOG.debug("Music tool failed", exc_info=True)
                return {"error": f"Music unavailable: {str(exc) or type(exc).__name__}"}

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
                "duration": spoken(
                    self._current.duration_s if self._current is not None else None, "s"
                ),
            }
        if "like" not in self.player.capabilities:
            return {"error": "This player does not support likes"}
        await self.player.like()
        return {"status": "Liked track"}

    def service_tools(self):
        string = {"type": "string"}
        definitions = {
            "play_music": (
                "Play music. kind=genre for a genre, era or mood: write the query as a "
                "descriptive playlist search such as '70s disco classics' or 'late night jazz "
                "bar'; it queues several songs. kind=artist or song plays that, then similar "
                "music.",
                {
                    "query": string,
                    "mood": string,
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
            )
            for name, (description, properties, required) in definitions.items()
        ]

    async def poll(self):
        """One poll, exposed for deterministic tests and alternative schedulers."""
        async with self._tools_lock:
            return await self._poll()

    async def _poll(self):
        self._current = await self.dj.current_track()
        self._available = self._current is not None
        if self._current is None:
            return None
        video_id = self._current.video_id
        if video_id == self._last_id:
            return None
        self._last_id = video_id
        await self.dj.top_up()
        if video_id in self.dj.announced:
            self.dj.announced.discard(video_id)
            return None
        if self.clock() - self._last_remark < self.settings["track_remark_cooldown_s"]:
            return None
        self._last_remark = self.clock()
        return GameEvent(
            "music_track",
            "ambient",
            {
                "summary": f"Now playing: {self._current.summary()}.",
                "ttl_s": 15,
                "video_id": video_id,
                "companion_service": True,
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

    async def speech_state(self, state):
        if not self._available and not self.dj.interrupting:
            return
        try:
            await self.dj.speech_state(state)
        except Exception:
            self._available = False
            raise

    async def aclose(self):
        try:
            await self.dj.aclose()
        finally:
            await self.player.aclose()
