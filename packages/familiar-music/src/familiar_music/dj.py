"""Selection, taste and one coordinator for all music interruptions."""

import asyncio
import random
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from itertools import chain

from familiar.interfaces import spoken

from .player import Track
from .settings import music_settings

# How far back "don't repeat" looks, for songs and for genre playlists.
RECENT_TRACKS = 60
RECENT_PLAYLISTS = 5
PLAYLIST_CHOICES = 3


@dataclass
class Selection:
    tracks: list[Track]
    reserve: list[Track] = field(default_factory=list)
    playlist: str = ""


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
            self.write(track.uri, artists)
            return
        self.tracks[track.uri] = artists

    def blocked(self, track):
        tracks, artists = self.values()
        return track.uri in tracks or any(a.casefold() in artists for a in track.artists)


class DJ:
    def __init__(
        self, player, searcher, *, settings=None, dislikes=None, clock=time.monotonic, rng=None
    ):
        self.player, self.searcher = player, searcher
        self.rng = rng or random.Random()
        self.settings = music_settings(settings)
        self.dislikes = dislikes or Dislikes()
        self.clock = clock
        self.last_seed = float("-inf")
        self.announced: set[str] = set()
        self.radio_seed: str | None = None
        self.reserve: list = []
        self.recent: deque[str] = deque(maxlen=RECENT_TRACKS)
        self.recent_playlists: deque[str] = deque(maxlen=RECENT_PLAYLISTS)
        self.base_volume: float | None = None
        self.level: float | None = None
        self.volume_chosen = False
        self.ducks = 0
        self._pause_owner: str | None = None
        self._pause_requested = False
        self._lock = asyncio.Lock()

    async def seed(self, query, mood=None, *, kind="song", calm=False):
        """Genres start a shuffled batch from a curated playlist; songs and artists start radio."""
        text = " ".join(filter(None, (query, mood)))
        pickers = {"genre": self._pick_playlist, "artist": self._pick_song, "song": self._pick_song}
        selection = await pickers.get(kind, self._pick_song)(text)
        if selection is None:
            return {"error": "No suitable music found"}
        return await self._start(selection, calm)

    def _fresh(self, tracks):
        return [t for t in tracks if not self.dislikes.blocked(t) and t.uri not in self.recent]

    async def _pick_song(self, text):
        hits = await self.searcher.songs(text, self.settings["search_limit"])
        allowed = [track for track in hits if not self.dislikes.blocked(track)]
        # Asking again should not replay the same top hit; fall back to it only if nothing is new.
        track = next(iter(self._fresh(allowed) + allowed), None)
        if track is None:
            return None
        return Selection([track])

    async def _pick_playlist(self, text):
        candidates = await self.searcher.playlists(text, self.settings["search_limit"])
        new = [p for p in candidates if p.playlist_id not in self.recent_playlists]
        # Relevance falls off fast past the first few results, so vary only among those.
        top = new[:PLAYLIST_CHOICES]
        self.rng.shuffle(top)
        for playlist in top + new[PLAYLIST_CHOICES:] + candidates:
            tracks = await self.searcher.playlist_tracks(
                playlist.playlist_id, self.settings["playlist_tracks"]
            )
            fresh = self._fresh(tracks)
            if not fresh:
                continue
            self.rng.shuffle(fresh)
            self.recent_playlists.append(playlist.playlist_id)
            batch = self.settings["genre_batch"]
            return Selection(fresh[:batch], fresh[batch:], playlist.title)
        return None

    async def _start(self, selection, calm):
        first, *rest = selection.tracks
        if "clear" in self.player.capabilities:
            await self.player.clear_upcoming()
        await self.player.play_now(first.uri)
        for track in rest:
            await self.player.enqueue(track.uri)
        self.recent.extend(track.uri for track in selection.tracks)
        self.reserve = list(selection.reserve)
        self._pause_owner = None
        self.announced.add(first.uri)
        self.radio_seed = first.uri
        self.last_seed = self.clock()
        level = await self._default_volume(calm)
        await self.top_up(first.uri, force=not rest)
        result = {"playing": first.summary(), "duration": spoken(first.duration_s, "s"), **level}
        if selection.playlist:
            result |= {"playlist": selection.playlist, "queued": f"{len(rest)} more songs"}
        return result

    async def _default_volume(self, calm):
        """Calm music always settles at calm_volume; plain play keeps a volume someone chose."""
        if self.volume_chosen and not calm:
            return {}
        key = {True: "calm_volume", False: "play_volume"}[calm]
        return await self._locked_volume(self.settings[key], None)

    async def top_up(self, uri=None, *, force=False):
        """Keep a few songs queued: the genre playlist's reserve first, then radio."""
        seed = uri or self.radio_seed
        if seed is None:
            return
        queued = set(await self.player.queue_ids())
        target = self.settings["queue_top_up"]
        needed = max(0, target - len(queued - {seed}))
        if force:
            needed = target
        if not needed:
            return
        queued.add(seed)
        self.reserve, needed = await self._enqueue_from(self.reserve, queued, needed)
        if not needed:
            return
        radio = await self.searcher.radio(
            seed, target + len(queued) + self.settings["search_limit"]
        )
        await self._enqueue_from(radio, queued, needed)

    async def _enqueue_from(self, pool, queued, needed):
        """Enqueue up to `needed` new tracks; returns (unused rest of the pool, still needed).

        The radio seed follows the last queued track so recommendations keep moving instead of
        running dry on one seed's short list once "don't repeat" has used it up.
        """
        for position, track in enumerate(pool):
            if not needed:
                return pool[position:], 0
            if track.uri in queued or not self._fresh([track]):
                continue
            await self.player.enqueue(track.uri)
            queued.add(track.uri)
            self.recent.append(track.uri)
            self.radio_seed = track.uri
            needed -= 1
        return [], needed

    async def reseed(self, *, calmer=False):
        if self.clock() - self.last_seed < self.settings["reseed_cooldown_s"]:
            return {"status": "Music selection is already refreshing"}
        if calmer:
            return await self.seed(self.settings["calmer_query"], kind="genre", calm=True)
        current = await self.player.now_playing()
        if current is None:
            return {"error": "Nothing is playing"}
        self.radio_seed = current.uri
        self.reserve = []  # "more like this" means the current song, not the last genre
        self.last_seed = self.clock()
        await self.top_up(current.uri, force=True)
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
            self.radio_seed = current.uri
        self._pause_owner = None
        await self.top_up(force=True)
        return {"status": f"Disliked and skipped {track.title}"}

    async def acquire_duck(self):
        if self.ducks:
            self.ducks += 1
            return
        self.base_volume = await self._owned_volume()
        self.level = self.base_volume
        self.ducks = 1
        try:
            await self.player.set_volume(self.base_volume * self.settings["duck_level"])
        except BaseException:
            await self.restore_volume()
            raise

    async def _owned_volume(self):
        """Keep the level we set unless the player moved away from it.

        Player reads lag writes and round, so trusting them blindly lets every duck shave a little
        off the restored volume until the music falls silent.
        """
        current = await self.player.get_volume()
        if self.level is None:
            return current
        ours = (self.level, self.level * self.settings["duck_level"])
        if any(abs(current - value) <= self.settings["volume_tolerance"] for value in ours):
            return self.level
        return current

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
        self.level = base
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
        """An explicit request; later plays keep it instead of the play_volume default."""
        self.volume_chosen = True
        return await self._locked_volume(level, change)

    async def _locked_volume(self, level, change):
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
            base = await self._owned_volume()
        value = level
        if value is None:
            value = base + change
        value = max(0, min(100, value))
        applied = value
        self.level = value
        if self.ducks:
            self.base_volume = value
            applied = value * self.settings["duck_level"]
        await self.player.set_volume(applied)
        return {"volume": spoken(value, "%")}

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
        self._pause_owner = current.uri
        await self.player.pause()

    async def _resume_owned(self):
        owner = self._pause_owner
        if owner is None:
            self._pause_requested = False
            return
        current = await self.player.now_playing()
        if current is not None and current.uri == owner and current.paused:
            await self.player.resume()
        # Keep ownership on failure so a later state transition/shutdown can restore it.
        self._pause_owner = None
        self._pause_requested = False

    def observe(self, current):
        if self._pause_owner is None or current is None:
            return
        if current.uri != self._pause_owner or not current.paused:
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
