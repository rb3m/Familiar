"""Selection, taste and one coordinator for all music interruptions."""

import asyncio
import random
import time
from collections import Counter, deque
from itertools import chain

from familiar.interfaces import spoken

from .settings import music_settings
from .sources import LocalSource, Selection, YouTubeSource, artist_key

# How far back "don't repeat" looks, for songs and for genre playlists.
RECENT_TRACKS = 60
RECENT_PLAYLISTS = 5


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

    def blocked(self, track, *, values=None):
        tracks, artists = values or self.values()
        return track.uri in tracks or any(a.casefold() in artists for a in track.artists)


class DJ:
    def __init__(
        self,
        player,
        searcher,
        *,
        settings=None,
        dislikes=None,
        clock=time.monotonic,
        rng=None,
        index=None,
        taste_read=None,
    ):
        self.player = player
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
        self.recent_artists: deque[str] = deque(maxlen=self.settings["artist_cooldown_songs"])
        self._heard_uri = None
        self.taste_read = taste_read or (lambda: [])
        self._pending_batch = None
        self.sources = {
            "ytm": YouTubeSource(
                searcher, self.settings, self.rng, self._fresh, self.dislikes, self.recent_playlists
            )
        }
        if index is not None:
            self.sources["local"] = LocalSource(
                index, self.settings, self.rng, self._fresh, self.recent_artists
            )
        self._radio_count = 0
        self._radio_track = None
        self.base_volume: float | None = None
        self.level: float | None = None
        self.volume_chosen = False
        self.ducks = 0
        self._pause_owner: str | None = None
        self._pause_requested = False
        self._lock = asyncio.Lock()

    def _available(self, prefix):
        available = getattr(self.player, "available", lambda p: True)
        return prefix in self.sources and available(prefix)

    def draw_lane(self):
        """Draw once per unrequested batch, independently of explicit requests."""
        weights = self.settings["blend"]
        return self.rng.choices(list(weights), weights=list(weights.values()), k=1)[0]

    async def seed(
        self, query, mood=None, *, kind="song", calm=False, genres=None, years=None, source=None
    ):
        text = " ".join(filter(None, (query, mood)))
        forced = {"library": "local", "youtube": "ytm"}.get(source)
        if source not in {None, "any", "library", "youtube"}:
            return {"error": "Unknown music source"}
        first = forced or "local"
        order = [first]
        if forced is None:
            order.append({"local": "ytm", "ytm": "local"}[first])
        for prefix in order:
            selection = await self._select(prefix, text, kind, genres, years)
            if selection is None:
                continue
            matches = selection.matches
            if (
                prefix == "local"
                and kind == "genre"
                and not forced
                and matches < self.settings["local_min_matches"]
            ):
                continue
            return await self._start(selection, calm)
        label = {"local": "library", "ytm": "YouTube Music"}.get(forced, "music")
        return {"error": f"No suitable {label} found or source unavailable"}

    async def _select(self, prefix, text, kind, genres=None, years=None):
        if not self._available(prefix):
            return None
        provider = self.sources[prefix]
        if kind == "genre":
            return await provider.genre(text, genres, years)
        picker = {"artist": provider.artist, "song": provider.song}.get(kind, provider.song)
        return await picker(text)

    def _fresh(self, tracks):
        values = self.dislikes.values()
        return [
            t
            for t in tracks
            if not self.dislikes.blocked(t, values=values) and t.uri not in self.recent
        ]

    async def _start(self, selection: Selection, calm, *, radio=False):
        first, *rest = selection.tracks
        prefix = first.uri.partition(":")[0]
        await self._play_selection(first, rest)
        self.recent.extend(track.uri for track in selection.tracks)
        self.reserve = list(selection.reserve)
        self._pending_batch = None
        self._remember_track(first)
        self.announced.add(first.uri)
        self.radio_seed = first.uri
        self._radio_track = first
        self._radio_count = {True: len(selection.tracks), False: 0}[radio]
        self.last_seed = self.clock()
        level = await self._default_volume(calm)
        await self.top_up(first.uri, force=not rest)
        result = {
            "playing": first.summary(),
            "duration": spoken(first.duration_s, "s"),
            "source": {"local": "library", "ytm": "YouTube Music"}[prefix],
            **level,
        }
        if selection.playlist:
            result |= {"playlist": selection.playlist, "queued": f"{len(rest)} more songs"}
        return result

    async def _play_selection(self, first, rest):
        async with self._lock:
            prefix = first.uri.partition(":")[0]
            active = getattr(self.player, "active", prefix)
            if active == prefix and "clear" in self.player.capabilities:
                await self.player.clear_upcoming()
            await self.player.play_now(first.uri)
            self._pause_owner = None
            if self._pause_requested:
                await self._pause_for_speech()
            await self._queue_selection(rest)

    async def _queue_selection(self, rest):
        for track in rest:
            await self.player.enqueue(track.uri)

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
        upcoming = set(await self.player.queue_ids())
        queued = set(upcoming)
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
        prefix = seed.partition(":")[0]
        if not self._available(prefix):
            return
        batch = self.settings["genre_batch"]
        if "local" in self.sources and self._radio_count >= batch:
            await self._next_radio_batch(prefix, upcoming)
            return
        limit = target + len(queued) + self.settings["search_limit"]
        selection = await self.sources[prefix].radio(seed, limit, queued | set(self.recent))
        if selection is None:
            return
        remaining = needed
        if "local" in self.sources:
            remaining = min(needed, batch - self._radio_count)
        _, left = await self._enqueue_from(selection.tracks, queued, remaining)
        self._radio_count += remaining - left

    async def _next_radio_batch(self, prefix, queued):
        selection = self._pending_batch
        if selection is None:
            selection = await self._lane_batch(self.draw_lane(), prefix)
        if selection is None:
            selection = await self._radio_batch(prefix, prefix)
        if selection is None:
            selection = await self._library_batch()
        if selection is None:
            selection = await self._radio_batch("ytm", prefix)
        if selection is None:
            return
        self._pending_batch = selection
        chosen = selection.tracks[0].uri.partition(":")[0]
        current = await self.player.now_playing()
        if chosen != prefix and (queued or current is not None):
            return
        selection.reserve = []
        if current is not None:
            await self._enqueue_from(selection.tracks, set(queued), len(selection.tracks))
            self._radio_count = len(selection.tracks)
            self._pending_batch = None
            return
        await self._start(selection, False, radio=True)

    async def _lane_batch(self, lane, prefix):
        if lane == "persona":
            return await self._persona_lane(prefix)
        if lane == "library":
            return await self._library_batch()
        return await self._radio_batch("ytm", prefix)

    async def _library_batch(self):
        if not self._available("local"):
            return None
        return await self.sources["local"].library()

    async def _persona_lane(self, prefix):
        tastes = self.taste_read()
        if not tastes:
            return await self._lane_batch("library", prefix)
        return await self._persona_batch(self.rng.choice(tastes))

    async def _persona_batch(self, text):
        selection = await self._select("local", text, "genre")
        matches = 0
        if selection is not None:
            matches = selection.matches
        if matches >= self.settings["local_min_matches"]:
            return selection
        return await self._select("ytm", text, "genre")

    async def _radio_batch(self, chosen, prefix):
        if not self._available(chosen):
            return None
        if chosen == prefix:
            return await self.sources[chosen].radio(
                self.radio_seed, self.settings["genre_batch"], set(self.recent)
            )
        track = self._radio_track
        if track is None:
            return None
        text = track.genre or ", ".join(track.artists)
        kind = {True: "genre", False: "artist"}[bool(track.genre)]
        selection = await self._select(chosen, text, kind)
        if selection is None:
            return None
        matches = selection.matches
        if chosen == "local" and kind == "genre" and matches < self.settings["local_min_matches"]:
            return None
        return selection

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
            self._radio_track = track
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
        self._radio_track = current
        self._radio_count = 0
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

    @property
    def paused_for_speech(self):
        return self._pause_owner is not None

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

    def _remember_track(self, current):
        if current is not None and current.uri != self._heard_uri:
            self._heard_uri = current.uri
            self.recent_artists.append(artist_key(current))

    def observe(self, current):
        self._remember_track(current)
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
