"""Backend-independent playback values and contract; search is separate."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Track:
    uri: str
    title: str
    artists: tuple[str, ...] = ()
    album: str = ""
    duration_s: float | None = None
    paused: bool = False
    year: int | None = None
    genre: str = ""

    def summary(self) -> str:
        return f"{self.title} by {', '.join(self.artists) or 'unknown artist'}"


@dataclass(frozen=True)
class Playlist:
    playlist_id: str
    title: str


class MusicPlayer(Protocol):
    capabilities: frozenset[str]

    async def now_playing(self) -> Track | None: ...
    async def play_now(self, uri: str) -> None: ...
    async def enqueue(self, uri: str) -> None: ...
    async def skip(self) -> None: ...
    async def pause(self) -> None: ...
    async def resume(self) -> None: ...
    async def get_volume(self) -> float: ...
    async def set_volume(self, level: float) -> None: ...
    async def like(self) -> None: ...
    async def dislike(self) -> None: ...
    async def queue_ids(self) -> list[str]: ...  # upcoming track uris only
    async def clear_upcoming(self) -> None: ...  # only when "clear" is in capabilities
