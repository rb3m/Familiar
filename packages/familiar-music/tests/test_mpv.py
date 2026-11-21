"""Offline mpv player tests: a fake JSON IPC peer; one live test needs real mpv+ffmpeg."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from familiar_music.mpv import MpvPlayer
from familiar_music.player import Track

REAL_RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", "")


@pytest.fixture(autouse=True)
def isolated_data(monkeypatch, tmp_path):
    monkeypatch.setenv("FAMILIAR_DATA_DIR", str(tmp_path / "data"))
    # macOS caps unix socket paths at 104 bytes and its pytest tmp_path is longer than that.
    runtime = tempfile.mkdtemp(prefix="fm-", dir="/tmp")
    monkeypatch.setenv("XDG_RUNTIME_DIR", runtime)
    yield
    shutil.rmtree(runtime, ignore_errors=True)


TRACKS = {
    "local:a": (Path("/music/a.mp3"), Track("local:a", "Song A", ("Artist A",))),
    "local:b": (Path("/music/b.flac"), Track("local:b", "Song B", ("Artist B",), duration_s=180.0)),
}


def resolver(uri):
    return TRACKS.get(uri)


class FakeMpv:
    """A minimal mpv JSON IPC peer over a unix socket, for the commands MpvPlayer uses."""

    def __init__(self, socket_path):
        self.socket_path = socket_path
        self.playlist = []
        self.position = -1
        self.paused = False
        self.volume = 50.0
        self.duration = 200.0
        self.commands = []
        self.server = None

    async def __aenter__(self):
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.clients = set()
        self.server = await asyncio.start_unix_server(self._handle, path=self.socket_path)
        return self

    async def __aexit__(self, *_):
        # 3.12 wait_closed also waits on connections; drop them ourselves instead.
        self.server.close()
        for writer in self.clients:
            writer.close()

    async def _handle(self, reader, writer):
        self.clients.add(writer)
        while line := await reader.readline():
            request = json.loads(line)
            response = self._dispatch(request.get("command", []))
            response["request_id"] = request.get("request_id")
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        self.clients.discard(writer)

    def _dispatch(self, command):
        self.commands.append(command)
        handler = getattr(self, "cmd_" + command[0].replace("-", "_"), None)
        if handler is None:
            return {"error": "unknown command"}
        return handler(*command[1:])

    def cmd_loadfile(self, path, mode="replace"):
        actions = {
            "append": lambda p: self.playlist.append(p),
            "append-play": self._append_play,
            "insert-next": lambda p: self.playlist.insert(self.position + 1, p),
            "replace": self._append_play,
        }
        actions[mode](path)
        return {"error": "success"}

    def _append_play(self, path):
        self.playlist.append(path)  # real mpv 0.41 stays idle; playlist-play-index starts it

    def cmd_playlist_play_index(self, index):
        if not 0 <= index < len(self.playlist):
            return {"error": "error running command"}
        self.position = index
        return {"error": "success"}

    def cmd_playlist_next(self):
        if self.position + 1 >= len(self.playlist):
            return {"error": "error running command"}
        self.position += 1
        return {"error": "success"}

    def cmd_playlist_remove(self, index):
        del self.playlist[index]
        return {"error": "success"}

    def cmd_stop(self):
        self.position = -1
        return {"error": "success"}

    def cmd_set_property(self, name, value):
        setattr(self, {"pause": "paused"}.get(name, name), value)
        return {"error": "success"}

    def cmd_get_property(self, name):
        values = {
            "pause": self.paused,
            "volume": self.volume,
            "duration": self.duration,
            "playlist-pos": self.position if self.position >= 0 else None,
            "playlist": [{"filename": p} for p in self.playlist],
        }
        return {"error": "success", "data": values[name]}


async def test_play_now_enqueue_skip_and_now_playing(tmp_path):
    player = MpvPlayer(resolver=resolver)
    async with FakeMpv(player.socket_path) as mpv:
        await player.play_now("local:a")
        assert ["loadfile", "/music/a.mp3", "append-play"] in mpv.commands
        playing = await player.now_playing()
        assert playing.title == "Song A"
        assert playing.duration_s == 200.0  # metadata lacks it: mpv's duration fills in
        assert not playing.paused

        await player.enqueue("local:b")
        assert await player.queue_ids() == ["local:b"]

        await player.skip()
        playing = await player.now_playing()
        assert playing.uri == "local:b"
        assert playing.duration_s == 180.0  # metadata wins over mpv's duration

        await player.skip()  # end of the list: playlist-next fails, so stop
        assert ["stop"] in mpv.commands
        assert await player.now_playing() is None
        assert player._process is None  # an answering socket is reused, never spawned
        await player.aclose()


async def test_play_now_mid_queue_jumps_the_queue(tmp_path):
    player = MpvPlayer(resolver=resolver)
    async with FakeMpv(player.socket_path) as mpv:
        mpv.playlist = ["/music/other.mp3"]
        mpv.position = 0
        await player.play_now("local:a")
        queue_writes = [c for c in mpv.commands if c[0] in {"loadfile", "playlist-next"}]
        assert queue_writes == [
            ["loadfile", "/music/a.mp3", "insert-next"],
            ["playlist-next"],
        ]
        assert (await player.now_playing()).uri == "local:a"
        await player.aclose()


async def test_pause_resume_and_volume_clamping(tmp_path):
    player = MpvPlayer(resolver=resolver)
    async with FakeMpv(player.socket_path) as mpv:
        await player.play_now("local:a")
        await player.pause()
        assert (await player.now_playing()).paused
        await player.resume()
        assert not (await player.now_playing()).paused
        await player.set_volume(20)
        assert await player.get_volume() == 20
        await player.set_volume(250)
        assert mpv.volume == 100.0
        mpv.volume = 130.0  # mpv can be pushed past 100 by other clients
        assert await player.get_volume() == 100
        await player.aclose()


async def test_clear_upcoming_keeps_current_and_history(tmp_path):
    player = MpvPlayer(resolver=resolver)
    async with FakeMpv(player.socket_path) as mpv:
        await player.play_now("local:a")
        await player.enqueue("local:b")
        await player.enqueue("local:a")
        await player.enqueue("local:b")
        assert await player.queue_ids() == ["local:b", "local:a", "local:b"]
        await player.clear_upcoming()
        assert mpv.playlist == ["/music/a.mp3"]
        assert mpv.position == 0
        assert await player.queue_ids() == []
        await player.aclose()


async def test_rejects_non_local_and_unknown_uris(tmp_path):
    player = MpvPlayer(resolver=resolver)
    with pytest.raises(ValueError, match="local:"):
        await player.play_now("ytm:abc")
    with pytest.raises(ValueError, match="local:"):
        await player.enqueue("ytm:abc")
    with pytest.raises(ValueError, match="Unknown local track"):
        await player.enqueue("local:missing")
    await player.aclose()


async def test_health_states(tmp_path):
    async def silent(reader, writer):
        await asyncio.Event().wait()

    missing = MpvPlayer({"executable": "mpv-definitely-not-installed"}, resolver=resolver)
    health = await missing.health()
    assert health.status == "not running"
    assert health.hint == "Install mpv"

    # Any existing executable passes the install check, so this runs without mpv installed.
    installed = {"executable": sys.executable}
    player = MpvPlayer({**installed, "call_timeout_s": 0.05}, resolver=resolver)
    player.socket_path.parent.mkdir(parents=True, exist_ok=True)
    server = await asyncio.start_unix_server(silent, path=player.socket_path)
    assert (await player.health()).status == "unavailable"
    server.close()  # 3.12 wait_closed would wait on the never-answering connection

    async with FakeMpv(player.socket_path):
        healthy = MpvPlayer(installed, resolver=resolver)
        assert (await healthy.health()).status == "ok"
        await healthy.aclose()
    await player.aclose()


@pytest.mark.live
async def test_real_mpv_plays_a_generated_file(tmp_path, monkeypatch):
    if shutil.which("mpv") is None or shutil.which("ffmpeg") is None:
        pytest.skip("live test needs mpv and ffmpeg")
    if not REAL_RUNTIME_DIR:
        pytest.skip("live test needs XDG_RUNTIME_DIR so mpv reaches the audio server")
    # mpv finds its audio server under the real XDG_RUNTIME_DIR, not tmp_path.
    monkeypatch.setenv("XDG_RUNTIME_DIR", REAL_RUNTIME_DIR)
    wav = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "1", str(wav)],
        check=True,
        capture_output=True,
    )
    player = MpvPlayer(
        {"socket_name": "mpv-live-test.sock"},
        resolver=lambda uri: (wav, Track(uri, "Tone", duration_s=1.0)),
    )
    await player.play_now("local:tone")
    await asyncio.sleep(0.5)
    playing = await player.now_playing()
    assert playing is not None
    assert playing.title == "Tone"
    await player.aclose()
    assert player._process.returncode is not None


async def test_play_now_unpauses_and_reconnects_after_a_dropped_connection(tmp_path):
    player = MpvPlayer(resolver=resolver)
    async with FakeMpv(player.socket_path) as mpv:
        await player.play_now("local:a")
        await player.pause()
        await player.play_now("local:b")
        assert not (await player.now_playing()).paused  # a new pick never starts paused

        for writer in list(mpv.clients):
            writer.close()  # mpv restarted or the socket dropped
        await asyncio.sleep(0.05)
        assert (await player.now_playing()).uri == "local:b"  # reconnected on the next call
        await player.aclose()
    assert ["quit"] in mpv.commands  # the socket is Familiar's, so a reused mpv is quit too
