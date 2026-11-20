"""Selection sources; lane policy and playback ownership stay in the DJ."""

import asyncio
import math
from collections import Counter
from dataclasses import dataclass, field
from itertools import chain
from typing import Protocol

from familiar.interfaces import load_data

from .library.index import LibraryIndex, accent_fold, primary_artist, tag_key
from .library.query import expand
from .player import Track

PLAYLIST_CHOICES = 3


@dataclass
class Selection:
    tracks: list[Track]
    reserve: list[Track] = field(default_factory=list)
    playlist: str = ""
    match_count: int | None = None
    match_artists: int = 0

    @property
    def matches(self):
        if self.match_count is not None:
            return self.match_count
        return len(self.tracks) + len(self.reserve)


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
            fresh = self._automatic(self._fresh(tracks))
            if not fresh:
                continue
            self.rng.shuffle(fresh)
            self.recent_playlists.append(playlist.playlist_id)
            batch = self.settings["genre_batch"]
            return Selection(fresh[:batch], fresh[batch:], playlist.title)
        return None

    def _automatic(self, tracks):
        return [
            t
            for t in tracks
            if t.duration_s is None or t.duration_s >= self.settings["min_track_s"]
        ]

    async def radio(self, seed_track, limit, exclude):
        tracks = await self.searcher.radio(seed_track, limit)
        fresh = [t for t in self._automatic(self._fresh(tracks)) if t.uri not in exclude]
        if not fresh:
            return None
        return Selection(fresh)


def artist_key(track, patterns=None):
    return accent_fold(primary_artist(next(iter(track.artists), ""), patterns))


class LocalSource:
    def __init__(self, index, settings, rng, fresh, recent_artists=()):
        self.index, self.settings, self.rng = index, settings, rng
        self._fresh = fresh
        self.recent_artists = recent_artists

    def _tracks(self, rows, *, automatic=False):
        tracks = self._fresh([LibraryIndex.track(row) for row in rows])
        if not automatic:
            return tracks
        return [t for t in tracks if (t.duration_s or 0) >= self.settings["min_track_s"]]

    def _behaviour(self, key, scores):
        played = scores.get((key, "played"), 0)
        skipped = scores.get((key, "skipped"), 0)
        factor = 1 + self.settings["played_gain"] * played - self.settings["skip_penalty"] * skipped
        return min(self.settings["taste_max"], max(self.settings["taste_min"], factor))

    def _weight(self, track, facts, terms, years, artist, key, occasional):
        tags, track_scores, artist_scores = facts
        relevant = sum(bool(term & tags[track.uri]) for term in terms)
        relevant += int(
            years is not None and track.year is not None and years[0] <= track.year <= years[1]
        )
        relevant += int(artist is not None and key == artist)
        behaviour = self._behaviour(track.uri, track_scores)
        behaviour *= math.sqrt(self._behaviour(key, artist_scores))
        cooldown = self.settings["artist_cooldown_songs"]
        recent = list(self.recent_artists)[max(0, len(self.recent_artists) - cooldown) :]
        recency = 1.0
        if key in recent:
            recency = self.settings["artist_cooldown_weight"]
        occasional_factor = 1.0
        if occasional:
            occasional_factor = self.settings["occasional_weight"]
        return max(1, relevant) * behaviour * recency * occasional_factor

    async def _batch(self, tracks, *, terms=(), years=None, artist=None, size=None, reserve=True):
        if not tracks:
            return None
        facts = await asyncio.to_thread(
            self.index.selection_facts, self.settings["taste_half_life_days"]
        )
        patterns = load_data("familiar_music", "library").value.get("featured_artist_patterns", [])
        artists = {t.uri: artist_key(t, patterns) for t in tracks}
        occasional_tags = {tag_key(g) for g in self.settings["occasional_genres"]}
        occasional_ids = {t.uri for t in tracks if facts[0][t.uri] & occasional_tags}
        weights = {
            t.uri: self._weight(
                t, facts, terms, years, artist, artists[t.uri], t.uri in occasional_ids
            )
            for t in tracks
        }
        size = size or self.settings["genre_batch"]
        pending = list(tracks)
        batches = []
        previous = None
        while (
            pending
            and (reserve or not batches)
            and sum(map(len, batches)) < self.settings["playlist_tracks"]
        ):
            batch = self._sample(pending, weights, artists, occasional_ids, size, previous)
            # A new batch may repeat an artist across its boundary when no alternative remains.
            batch = batch or self._sample(pending, weights, artists, occasional_ids, size, None)
            if not batch:
                break
            batches.append(batch)
            used = {t.uri for t in batch}
            pending = [t for t in pending if t.uri not in used]
            previous = artists[batch[-1].uri]
        if not batches:
            return None
        return Selection(
            batches[0],
            list(chain.from_iterable(batches[1:])),
            match_count=len(tracks),
            match_artists=len(set(artists.values()) - {""}),
        )

    def _sample(self, pending, weights, artists, occasional_ids, size, previous):
        result, counts = [], Counter()
        occasional = False
        cap = max(1, math.ceil(size * self.settings["artist_batch_share"]))
        used = set()
        for _ in range(size):
            eligible = [
                t
                for t in pending
                if counts[artists[t.uri]] < cap
                and t.uri not in used
                and not (occasional and t.uri in occasional_ids)
            ]
            spaced = [t for t in eligible if artists[t.uri] != previous]
            # No adjacent repeats, even for small or explicit single-artist pools.
            if not spaced:
                break
            track = self.rng.choices(spaced, weights=[weights[t.uri] for t in spaced], k=1)[0]
            result.append(track)
            used.add(track.uri)
            previous = artists[track.uri]
            counts[previous] += 1
            occasional |= track.uri in occasional_ids
        return result

    async def song(self, text):
        tracks = self._tracks(await asyncio.to_thread(self.index.search, text))
        if not tracks:
            return None
        return Selection(tracks[:1])

    async def artist(self, name):
        rows = await asyncio.to_thread(self.index.tracks_by_artist, name)
        return await self._batch(self._tracks(rows), artist=accent_fold(primary_artist(name)))

    async def library(self):
        rows = await asyncio.to_thread(self.index.filter)
        return await self._batch(self._tracks(rows, automatic=True))

    async def genre(self, text, genres=None, years=None):
        vocabulary = {
            tag_key(row["tag"])
            for row in await asyncio.to_thread(self.index.tag_vocabulary, limit=10000)
        }
        expanded, inferred_years, terms = expand(text, vocabulary)
        merged = list(dict.fromkeys([*expanded, *(genres or [])]))
        terms = list(dict.fromkeys([*terms, *(frozenset([tag_key(g)]) for g in (genres or []))]))
        bounds = years or inferred_years
        if not merged and bounds is None:
            return None
        rows = await asyncio.to_thread(
            self.index.filter,
            genres=merged,
            years=bounds,
            artist_tag_min_share=self.settings["artist_tag_min_share"],
        )
        return await self._batch(self._tracks(rows, automatic=True), terms=terms, years=bounds)

    async def radio(self, seed_track, limit, exclude):
        rows = await asyncio.to_thread(
            self.index.radio_candidates, seed_track, exclude=[*exclude, seed_track]
        )
        resolved = await asyncio.to_thread(self.index.resolve, seed_track)
        if resolved is None:
            return None
        seed = resolved[1]
        facts = await asyncio.to_thread(
            self.index.selection_facts, self.settings["taste_half_life_days"]
        )
        terms = [frozenset([tag]) for tag in facts[0][seed_track]]
        years = None
        if seed.year is not None:
            years = (seed.year - 5, seed.year + 5)
        selection = await self._batch(
            self._tracks(rows, automatic=True),
            terms=terms,
            years=years,
            artist=artist_key(seed),
            size=limit,
            reserve=False,
        )
        if selection is None:
            return None
        return Selection(selection.tracks)
