"""Selection, taste and one coordinator for all music interruptions."""

import asyncio
import time
from collections import Counter
from itertools import chain

from .settings import music_settings


class Dislikes:
    """MemoryStore callbacks are injected by core; disabled memory stays local."""

    def __init__(self, read=None, write=None):
        self.read, self.write = read, write
        self.tracks: dict[str, tuple[str, ...]] = {}

    def values(self):
        if self.read is not None:
            return self.read()
        counts = Counter(chain.from_iterable(self.tracks.values()))
        repeated = {artist for artist, count in counts.items() if count >= 2}
        return set(self.tracks), repeated

    def add(self, track):
        artists = tuple(dict.fromkeys(artist.casefold() for artist in track.artists))
        if self.write is not None:
            self.write(track.video_id, artists)
            return
        self.tracks[track.video_id] = artists

    def blocked(self, track):
        tracks, artists = self.values()
        return track.video_id in tracks or any(a.casefold() in artists for a in track.artists)


class DJ:
    def __init__(self, player, searcher, *, settings=None, dislikes=None, clock=time.monotonic):
        self.player, self.searcher = player, searcher
        self.settings = music_settings(settings)
        self.dislikes = dislikes or Dislikes()
        self.clock = clock
        self.last_seed = float("-inf")
        self.announced: set[str] = set()
        self.radio_seed: str | None = None
        self.base_volume: float | None = None
        self.ducks = 0
        self._pause_owner: str | None = None
        self._pause_requested = False
        self._lock = asyncio.Lock()

    async def seed(self, query, mood=None):
        hits = await self.searcher.songs(
            " ".join(filter(None, (query, mood))), self.settings["search_limit"]
        )
        track = next((track for track in hits if not self.dislikes.blocked(track)), None)
        if track is None:
            return {"error": "No suitable music found"}
        await self.player.play_now(track.video_id)
        self._pause_owner = None
        self.announced.add(track.video_id)
        self.radio_seed = track.video_id
        self.last_seed = self.clock()
        await self.top_up(track.video_id, force=True)
        return {"playing": track.summary()}

    async def top_up(self, video_id=None, *, force=False):
        seed = video_id or self.radio_seed
        if seed is None:
            return
        queued = set(await self.player.queue_ids())
        queued.add(seed)
        target = self.settings["queue_top_up"]
        needed = max(0, target - len(queued - {seed}))
        if force:
            needed = target
        if not needed:
            return
        radio = await self.searcher.radio(
            seed, target + len(queued) + self.settings["search_limit"]
        )
        for track in radio:
            if track.video_id in queued or self.dislikes.blocked(track):
                continue
            await self.player.enqueue(track.video_id)
            queued.add(track.video_id)
            needed -= 1
            if not needed:
                break

    async def reseed(self, *, calmer=False):
        if self.clock() - self.last_seed < self.settings["reseed_cooldown_s"]:
            return {"status": "Music selection is already refreshing"}
        if calmer:
            return await self.seed(self.settings["calmer_query"])
        current = await self.player.now_playing()
        if current is None:
            return {"error": "Nothing is playing"}
        self.radio_seed = current.video_id
        self.last_seed = self.clock()
        await self.top_up(current.video_id, force=True)
        return {"status": "Queued more like this"}

    async def dislike(self):
        track = await self.player.now_playing()
        if track is None:
            return {"error": "Nothing is playing"}
        self.dislikes.add(track)
        try:
            if "dislike" in self.player.capabilities:
                await self.player.dislike()
        finally:
            await self.player.skip()
        current = await self.player.now_playing()
        self.radio_seed = None
        if current is not None:
            self.radio_seed = current.video_id
        self._pause_owner = None
        await self.top_up(force=True)
        return {"status": f"Disliked and skipped {track.title}"}

    async def acquire_duck(self):
        if self.ducks:
            self.ducks += 1
            return
        self.base_volume = await self.player.get_volume()
        self.ducks = 1
        try:
            await self.player.set_volume(self.base_volume * self.settings["duck_level"])
        except BaseException:
            await self.restore_volume()
            raise

    async def release_duck(self):
        self.ducks = max(0, self.ducks - 1)
        if not self.ducks:
            await self.restore_volume()

    async def restore_volume(self):
        self.ducks = 0
        if self.base_volume is None:
            return
        base = self.base_volume
        await self.player.set_volume(base)
        self.base_volume = None

    @property
    def interrupting(self):
        return self.base_volume is not None or self._pause_requested

    async def current_track(self):
        """Read and observe atomically with respect to speech pause ownership."""
        async with self._lock:
            current = await self.player.now_playing()
            self.observe(current)
            return current

    async def skip(self):
        async with self._lock:
            self._pause_owner = None
            await self.player.skip()
        return {"status": "Skipped track"}

    async def volume(self, level=None, change=None):
        async with self._lock:
            try:
                return await self._volume(level, change)
            except BaseException:
                await self.restore_volume()
                raise

    async def _volume(self, level, change):
        if (level is None) == (change is None):
            return {"error": "Specify exactly one of level or change"}
        base = self.base_volume
        if base is None:
            base = await self.player.get_volume()
        value = level
        if value is None:
            value = base + change
        value = max(0, min(100, value))
        applied = value
        if self.ducks:
            self.base_volume = value
            applied = value * self.settings["duck_level"]
        await self.player.set_volume(applied)
        return {"volume": value}

    async def pause(self):
        async with self._lock:
            self._pause_owner = None
            await self.player.pause()
        return {"status": "Music paused"}

    async def resume(self):
        async with self._lock:
            self._pause_owner = None
            await self.player.resume()
        return {"status": "Music resumed"}

    async def _pause_for_speech(self):
        self._pause_requested = True
        current = await self.player.now_playing()
        if current is None or current.paused:
            return
        # Remember ownership before the request: a cancelled request may have taken effect.
        self._pause_owner = current.video_id
        await self.player.pause()

    async def _resume_owned(self):
        owner = self._pause_owner
        if owner is None:
            self._pause_requested = False
            return
        current = await self.player.now_playing()
        if current is not None and current.video_id == owner and current.paused:
            await self.player.resume()
        # Keep ownership on failure so a later state transition/shutdown can restore it.
        self._pause_owner = None
        self._pause_requested = False

    def observe(self, current):
        if self._pause_owner is None or current is None:
            return
        if current.video_id != self._pause_owner or not current.paused:
            self._pause_owner = None

    async def speech_state(self, state):
        async with self._lock:
            try:
                await self._apply_speech(state)
            except BaseException:
                try:
                    await self.restore_volume()
                finally:
                    await self._resume_owned()
                raise

    async def _apply_speech(self, state):
        pause = state.critical or (state.speaking and state.event_kind == "music_track")
        count = int(state.listening) + int(state.speaking)
        if pause and not self._pause_requested:
            await self.restore_volume()
            await self._pause_for_speech()
            return
        if pause:
            return
        if count and not self.ducks:
            await self.acquire_duck()
        if self._pause_requested:
            await self._resume_owned()
        if not count and self.ducks:
            await self.restore_volume()
        self.ducks = count

    async def aclose(self):
        try:
            await self.restore_volume()
        finally:
            await self._resume_owned()
