"""Unauthenticated YouTube Music discovery, off the event loop."""

import asyncio

from familiar.interfaces import load_data
from ytmusicapi import YTMusic

from .player import Playlist, Track

# Curated YouTube Music playlists first; community ones only when nothing curated matches.
PLAYLIST_FILTERS = ("featured_playlists", "community_playlists")


def _track(item):
    duration = item.get("duration_seconds")
    if duration is None and item.get("duration"):
        parts = str(item["duration"]).split(":")
        try:
            duration = sum(float(part) * 60**i for i, part in enumerate(reversed(parts)))
        except ValueError:
            duration = None
    album = item.get("album") or {}
    return Track(
        f"ytm:{item['videoId']}",
        item.get("title", "Unknown track"),
        tuple(artist["name"] for artist in item.get("artists", []) if artist.get("name")),
        album.get("name", ""),
        duration,
    )


class Searcher:
    def __init__(self, client=None, *, timeout=None):
        self.client = client or YTMusic()
        self.timeout = timeout or load_data("familiar_music", "music").value["search_timeout_s"]

    async def _call(self, function, *args, **kwargs):
        async with asyncio.timeout(self.timeout):
            return await asyncio.to_thread(function, *args, **kwargs)

    async def songs(self, query, limit):
        results = await self._call(self.client.search, query, filter="songs", limit=limit)
        return [_track(item) for item in results if item.get("videoId")][:limit]

    async def radio(self, uri, limit):
        result = await self._call(
            self.client.get_watch_playlist, videoId=uri.removeprefix("ytm:"), limit=limit
        )
        return [_track(item) for item in result.get("tracks", []) if item.get("videoId")][:limit]

    async def playlists(self, query, limit):
        """Playlist candidates for a genre or mood, best sources first."""
        found = []
        for kind in PLAYLIST_FILTERS:
            results = await self._call(self.client.search, query, filter=kind, limit=limit)
            found += [_playlist(item) for item in results if item.get("browseId")]
        return found

    async def playlist_tracks(self, playlist_id, limit):
        result = await self._call(self.client.get_playlist, playlist_id, limit=limit)
        return [_track(item) for item in result.get("tracks", []) if item.get("videoId")][:limit]


def _playlist(item):
    return Playlist(item["browseId"], item.get("title", "Untitled playlist"))
