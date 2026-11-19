"""Selection sources; lane policy and playback ownership stay in the DJ."""

from dataclasses import dataclass, field
from typing import Protocol

from .library.index import LibraryIndex, tag_key
from .library.query import expand
from .player import Track

PLAYLIST_CHOICES = 3


@dataclass
class Selection:
    tracks: list[Track]
    reserve: list[Track] = field(default_factory=list)
    playlist: str = ""


class Source(Protocol):
    async def song(self, text: str) -> Selection | None: ...
    async def artist(self, name: str) -> Selection | None: ...
    async def genre(self, text: str, genres=None, years=None) -> Selection | None: ...
    async def radio(self, seed_track: str, limit: int, exclude) -> Selection | None: ...


class YouTubeSource:
    def __init__(self, searcher, settings, rng, fresh, dislikes, recent_playlists):
        self.searcher, self.settings, self.rng = searcher, settings, rng
        self._fresh, self.dislikes = fresh, dislikes
        self.recent_playlists = recent_playlists

    async def song(self, text):
        hits = await self.searcher.songs(text, self.settings["search_limit"])
        allowed = [track for track in hits if not self.dislikes.blocked(track)]
        # Explicit repeats retain the original YouTube fallback when all hits are recent.
        track = next(iter(self._fresh(allowed) + allowed), None)
        if track is None:
            return None
        return Selection([track])

    async def artist(self, name):
        return await self.song(name)

    async def genre(self, text, genres=None, years=None):
        candidates = await self.searcher.playlists(text, self.settings["search_limit"])
        new = [p for p in candidates if p.playlist_id not in self.recent_playlists]
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

    async def radio(self, seed_track, limit, exclude):
        tracks = await self.searcher.radio(seed_track, limit)
        fresh = [t for t in self._fresh(tracks) if t.uri not in exclude]
        if not fresh:
            return None
        return Selection(fresh)


def space_artists(tracks):
    """Spread the largest artist groups first, randomising ties before this step."""
    groups = {}
    for track in tracks:
        groups.setdefault(tuple(a.casefold() for a in track.artists), []).append(track)
    result = []
    previous = None
    while groups:
        candidates = [key for key in groups if key != previous] or list(groups)
        key = max(candidates, key=lambda artist: len(groups[artist]))
        result.append(groups[key].pop())
        previous = key
        if not groups[key]:
            del groups[key]
    return result


class LocalSource:
    def __init__(self, index, settings, rng, fresh):
        self.index, self.settings, self.rng = index, settings, rng
        self._fresh = fresh

    def _tracks(self, rows):
        return self._fresh([LibraryIndex.track(row) for row in rows])

    def _batch(self, tracks):
        if not tracks:
            return None
        self.rng.shuffle(tracks)
        spaced = space_artists(tracks)
        batch = self.settings["genre_batch"]
        return Selection(spaced[:batch], spaced[batch:])

    async def song(self, text):
        tracks = self._tracks(self.index.search(text))
        if not tracks:
            return None
        return Selection(tracks[:1])

    async def artist(self, name):
        return self._batch(self._tracks(self.index.tracks_by_artist(name)))

    async def genre(self, text, genres=None, years=None):
        vocabulary = {tag_key(row["tag"]) for row in self.index.tag_vocabulary(limit=10000)}
        expanded, inferred_years = expand(text, vocabulary)
        merged = list(dict.fromkeys([*expanded, *(genres or [])]))
        bounds = years or inferred_years
        # Unrecognised text must not accidentally select the entire library.
        if not merged and bounds is None:
            return None
        tracks = self._tracks(self.index.filter(genres=merged, years=bounds))
        return self._batch(tracks)

    async def radio(self, seed_track, limit, exclude):
        rows = self.index.radio_candidates(seed_track, exclude=[*exclude, seed_track])
        tracks = self._tracks(rows)
        selection = self._batch(tracks)
        if selection is None:
            return None
        return Selection((selection.tracks + selection.reserve)[:limit])
