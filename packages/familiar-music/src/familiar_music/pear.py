"""Bounded Pear Desktop REST calls and private, persistent authorisation."""

import asyncio
import os
import tempfile
from itertools import pairwise
from pathlib import Path

import httpx

from .player import Health, Track
from .settings import pear_settings


class PearPlayer:
    capabilities = frozenset({"like", "dislike", "queue", "clear", "volume"})

    def __init__(self, settings=None, *, transport=None, state_dir=None):
        self.data = pear_settings(settings)
        root = Path(state_dir or os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        self.token_path = root / self.data["token_path"]
        self.token = ""
        if self.token_path.exists():
            self.token = self.token_path.read_text().strip()
        self.client = httpx.AsyncClient(base_url=self.data["base_url"], transport=transport)
        self._auth_lock = asyncio.Lock()

    async def _auth(self, old_token):
        async with self._auth_lock:
            if self.token and self.token != old_token:
                return
            timeout = self.data["auth_timeout_s"]
            async with asyncio.timeout(timeout):
                response = await self.client.post(
                    self.data["endpoints"]["auth"].format(id=self.data["client_id"]),
                    timeout=timeout,
                )
            response.raise_for_status()
            token = response.json()["accessToken"]
            if not isinstance(token, str) or not token:
                raise ValueError("Pear returned an empty access token")
            self._store_token(token)
            self.token = token

    def _store_token(self, token):
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self.token_path.parent, delete=False
            ) as f:
                temporary = Path(f.name)
                os.fchmod(f.fileno(), 0o600)
                f.write(token)
            temporary.replace(self.token_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def _send(self, method, kind, payload, path):
        timeout = self.data["call_timeout_s"]
        async with asyncio.timeout(timeout):
            return await self.client.request(
                method,
                self.data["endpoints"][kind].format(**path),
                json=payload,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=timeout,
            )

    async def _request(self, method, kind, payload=None, **path):
        if not self.token:
            await self._auth("")
        token = self.token
        response = await self._send(method, kind, payload, path)
        if response.status_code in {401, 403}:
            await self._auth(token)
            response = await self._send(method, kind, payload, path)
        response.raise_for_status()
        if response.status_code == 204:
            return None
        if not response.content:
            return None
        return response.json()

    async def health(self):
        try:
            track = await self.now_playing()
            if track is None:
                return Health("needs first play", "Press play once in YouTube Music")
            return Health("ok")
        except httpx.ConnectError:
            return Health("not running", "Open YouTube Music")
        except (httpx.TimeoutException, TimeoutError):
            return Health(
                "needs restart", "Restart YouTube Music after enabling the API Server plugin"
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403}:
                return Health("not authorised", "Allow the familiar client in YouTube Music")
            return Health("unavailable", "Open YouTube Music")

    async def now_playing(self):
        data = await self._request("GET", "song")
        if not data or not data.get("videoId"):
            return None
        return Track(
            f"ytm:{data['videoId']}",
            data.get("title", "Unknown track"),
            tuple(a.strip() for a in data.get("artist", "").split(",") if a.strip()),
            data.get("album", ""),
            data.get("songDuration"),
            data.get("isPaused", False),
        )

    async def play_now(self, uri):
        await self._request(
            "POST",
            "queue",
            {
                "videoId": _video_id(uri),
                "insertPosition": self.data["insert_now"],
            },
        )
        await self._until_queued(uri)
        await self.skip()

    async def _until_queued(self, uri):
        """Pear adds songs asynchronously; skipping before one lands plays the wrong song."""
        async with asyncio.timeout(self.data["insert_wait_s"]):
            while uri not in await self.queue_ids():
                await asyncio.sleep(self.data["insert_poll_s"])

    async def enqueue(self, uri):
        await self._request(
            "POST",
            "queue",
            {
                "videoId": _video_id(uri),
                "insertPosition": self.data["insert_end"],
            },
        )

    async def skip(self):
        await self._request("POST", "next")

    async def pause(self):
        await self._request("POST", "pause")

    async def resume(self):
        await self._request("POST", "play")

    async def get_volume(self):
        data = await self._request("GET", "volume")
        return _written_level(
            float(data.get("volume", data.get("state"))), self.data["volume_curve"]
        )

    async def set_volume(self, level):
        await self._request("POST", "volume", {"volume": max(0, min(100, level))})

    async def like(self):
        await self._request("POST", "like")

    async def dislike(self):
        await self._request("POST", "dislike")

    async def _queue(self):
        """Renderers for the whole queue and the index of the current song (-1 if none)."""
        data = await self._request("GET", "queue")
        entries = data or []
        if isinstance(data, dict):
            entries = data.get("items", [])
        renderers = [_renderer(entry) for entry in entries]
        current = next((i for i, r in enumerate(renderers) if r.get("selected")), -1)
        return renderers, current

    async def queue_ids(self):
        """Songs still to play after the current one; played history is not counted."""
        renderers, current = await self._queue()
        return [f"ytm:{video}" for r in renderers[current + 1 :] if (video := r.get("videoId"))]

    async def clear_upcoming(self):
        """Drop what was queued after the current song, last first so indices stay valid."""
        renderers, current = await self._queue()
        if current < 0:
            return
        for index in range(len(renderers) - 1, current, -1):
            await self._request("DELETE", "queue_item", index=index)

    async def aclose(self):
        await self.client.aclose()


def _written_level(read, curve):
    """Map a read-back volume to the level that was written, interpolating between points."""
    for (low_written, low_read), (high_written, high_read) in pairwise(curve):
        if read > high_read:
            continue
        share = max(0.0, (read - low_read) / (high_read - low_read))
        return low_written + share * (high_written - low_written)
    return curve[-1][0]


def _renderer(entry):
    """Pear queue items wrap YouTube's playlistPanelVideoRenderer, sometimes twice."""
    wrapper = entry.get("playlistPanelVideoWrapperRenderer", {}).get("primaryRenderer", entry)
    return wrapper.get("playlistPanelVideoRenderer", wrapper)


def _video_id(uri):
    """Translate a supported track URI at the Pear API boundary."""
    if not uri.startswith("ytm:"):
        raise ValueError(f"Pear only supports ytm: URIs: {uri}")
    return uri.removeprefix("ytm:")
