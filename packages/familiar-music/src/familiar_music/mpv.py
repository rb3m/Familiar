"""Local-library playback through an mpv subprocess on its JSON IPC socket."""

import asyncio
import contextlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from .player import Health, Track
from .settings import mpv_settings

# resolver(uri) -> (file path, tag metadata), or None when the uri is unknown.
Resolver = Callable[[str], "tuple[Path, Track] | None"]


class MpvError(Exception):
    """mpv refused a command or the IPC connection dropped."""


class MpvPlayer:
    capabilities = frozenset({"queue", "clear", "volume"})

    def __init__(self, settings=None, *, resolver: Resolver):
        self.data = mpv_settings(settings)
        self._resolver = resolver
        runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
        self.socket_path = Path(runtime) / self.data["socket_dir"] / self.data["socket_name"]
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_request = 0
        self._loaded: dict[str, str] = {}  # mpv playlist path -> local: uri
        self._start_lock = asyncio.Lock()

    async def play_now(self, uri):
        """Play the track now; while idle it just starts, mid-queue it jumps the queue."""
        path = self._resolve_path(uri)
        playlist, position = await self._playlist()
        await self._request("set_property", "pause", False)
        if position is not None and position >= 0:
            await self._request("loadfile", path, "insert-next")
            await self._request("playlist-next")
            return
        # mpv's append-play does not leave idle; playlist-play-index starts playback.
        await self._request("loadfile", path, "append-play")
        await self._request("playlist-play-index", len(playlist))

    async def enqueue(self, uri):
        await self._request("loadfile", self._resolve_path(uri), "append")

    async def skip(self):
        try:
            await self._request("playlist-next")
        except MpvError:  # end of the list: just stop
            await self._request("stop")

    async def pause(self):
        await self._request("set_property", "pause", True)

    async def resume(self):
        await self._request("set_property", "pause", False)

    async def get_volume(self):
        volume = await self._get("volume")
        return max(0.0, min(100.0, float(volume or 0.0)))

    async def set_volume(self, level):
        # mpv's volume property can go past 100; the player's 0-100 scale may not.
        await self._request("set_property", "volume", max(0.0, min(100.0, float(level))))

    async def queue_ids(self):
        """Uris of playlist entries after the current one; everything when idle."""
        playlist, position = await self._playlist()
        start = position + 1 if position is not None and position >= 0 else 0
        return [uri for e in playlist[start:] if (uri := self._loaded.get(e.get("filename")))]

    async def clear_upcoming(self):
        """Drop entries after the current one, last first so indices stay valid."""
        playlist, position = await self._playlist()
        if position is None or position < 0:
            return
        for index in range(len(playlist) - 1, position, -1):
            await self._request("playlist-remove", index)

    async def now_playing(self):
        playlist, position = await self._playlist()
        if position is None or not 0 <= position < len(playlist):
            return None
        uri = self._loaded.get(playlist[position].get("filename"))
        if uri is None:  # loaded by whoever owned the socket before us
            return None
        resolved = self._resolver(uri)
        if resolved is None:
            return None
        _, track = resolved
        duration = track.duration_s
        if duration is None:
            duration = await self._get_optional("duration")
        paused = await self._get_optional("pause")
        return replace(track, duration_s=duration, paused=bool(paused))

    async def health(self):
        if shutil.which(self.data["executable"]) is None:
            return Health("not running", "Install mpv")
        try:
            await self._request("get_property", "playlist-pos")
            return Health("ok")
        except Exception as exc:
            return Health("unavailable", f"mpv did not answer: {exc}")

    async def aclose(self):
        # Only Familiar uses this socket, so a reused orphan is ours to quit as well.
        if self._writer is not None:
            await self._quit_process()
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(OSError, TimeoutError):
                async with asyncio.timeout(self.data["quit_timeout_s"]):
                    await self._writer.wait_closed()
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        self._reader = self._writer = self._reader_task = None

    def _resolve_path(self, uri):
        if not uri.startswith("local:"):
            raise ValueError(f"mpv only supports local: URIs: {uri}")
        resolved = self._resolver(uri)
        if resolved is None:
            raise ValueError(f"Unknown local track: {uri}")
        path, _ = resolved
        self._loaded[str(path)] = uri
        return str(path)

    async def _playlist(self):
        """All entries and the index of the current one (None or -1 when idle)."""
        playlist = await self._get("playlist") or []
        position = await self._get("playlist-pos")
        return playlist, position if isinstance(position, int) else -1

    async def _get(self, name):
        return await self._request("get_property", name)

    async def _get_optional(self, name):
        with contextlib.suppress(MpvError):
            return await self._get(name)
        return None

    async def _request(self, *command):
        async with asyncio.timeout(self.data["call_timeout_s"]):
            await self._ensure_connection()
            return await self._roundtrip(list(command))

    async def _roundtrip(self, command):
        self._next_request += 1
        request_id = self._next_request
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload = json.dumps({"command": command, "request_id": request_id})
        self._writer.write(payload.encode() + b"\n")
        await self._writer.drain()
        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def _ensure_connection(self):
        async with self._start_lock:
            if self._writer is not None:
                return
            try:
                await self._connect()  # a previous Familiar's orphan: reuse it
                return
            except OSError:
                await self._spawn()
                await self._connect()

    async def _connect(self):
        self._reader, self._writer = await asyncio.open_unix_connection(self.socket_path)
        self._reader_task = asyncio.create_task(self._read_loop())
        await self._roundtrip(["get_property", "playlist-pos"])  # the peer must answer

    async def _spawn(self):
        executable = shutil.which(self.data["executable"])
        if executable is None:
            raise MpvError(f"{self.data['executable']} is not installed")
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.socket_path.parent, 0o700)
        self.socket_path.unlink(missing_ok=True)
        argv = [executable, *self.data["args"], f"--input-ipc-server={self.socket_path}"]
        self._process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        while not self.socket_path.exists():
            if self._process.returncode is not None:
                raise MpvError("mpv exited before creating its IPC socket")
            await asyncio.sleep(self.data["spawn_poll_s"])

    async def _read_loop(self):
        """Match responses to pending futures by request_id; events are ignored."""
        try:
            while line := await self._reader.readline():
                self._deliver(json.loads(line))
        except (OSError, ValueError):
            pass
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(MpvError("mpv IPC connection lost"))
            self._pending.clear()
            # Reconnect (or respawn) on the next call instead of writing to a dead socket.
            writer, self._reader, self._writer = self._writer, None, None
            if writer is not None:
                writer.close()

    def _deliver(self, message):
        future = self._pending.get(message.get("request_id"))
        if future is None or future.done():
            return
        error = message.get("error", "success")
        if error == "success":
            future.set_result(message.get("data"))
            return
        future.set_exception(MpvError(error))

    async def _wait_process(self):
        if self._process is not None:
            await self._process.wait()

    async def _quit_process(self):
        with contextlib.suppress(MpvError, OSError, TimeoutError):
            async with asyncio.timeout(self.data["quit_timeout_s"]):
                await self._roundtrip(["quit"])
                await self._wait_process()
                return
        if self._process is None or self._process.returncode is not None:
            return
        self._process.terminate()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(self.data["quit_timeout_s"]):
                await self._process.wait()
