"""Offline music contract tests: fake HTTP, discovery, clock and playback."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from familiar.interfaces import SpeechState, ToolCall
from familiar.memory.store import MemoryStore
from familiar.tools import ToolRegistry
from familiar_music.dj import DJ, Dislikes
from familiar_music.pear import Health, PearPlayer
from familiar_music.player import Track
from familiar_music.search import Searcher
from familiar_music.service import PearMusicService


@pytest.fixture(autouse=True)
def isolated_data(monkeypatch, tmp_path):
    monkeypatch.setenv("FAMILIAR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


async def test_token_storage_reauth_and_play_order(tmp_path):
    calls = []
    tokens = iter(["first", "second"])
    reject = True

    def handle(request):
        nonlocal reject
        calls.append((request.method, request.url.path, request.content))
        if request.url.path == "/auth/familiar":
            return httpx.Response(200, json={"accessToken": next(tokens)})
        if request.url.path.endswith("queue") and reject:
            reject = False
            return httpx.Response(401)
        return httpx.Response(204)

    player = PearPlayer(transport=httpx.MockTransport(handle))
    await player.play_now("seed")
    assert [path for _, path, _ in calls] == [
        "/auth/familiar",
        "/api/v1/queue",
        "/auth/familiar",
        "/api/v1/queue",
        "/api/v1/next",
    ]
    assert json.loads(calls[3][2]) == {
        "videoId": "seed",
        "insertPosition": "INSERT_AFTER_CURRENT_VIDEO",
    }
    assert player.token_path.read_text() == "second"
    assert player.token_path.stat().st_mode & 0o777 == 0o600
    await player.aclose()
    saved = PearPlayer(transport=httpx.MockTransport(handle))
    assert saved.token == "second"
    await saved.aclose()


@pytest.mark.parametrize(
    "failure,status,hint",
    [
        (httpx.ReadTimeout("hung"), "needs restart", "Restart"),
        (httpx.ConnectError("refused"), "not running", "Open"),
        (204, "needs first play", "Press play"),
        (403, "not authorised", "Allow"),
    ],
)
async def test_health(failure, status, hint):
    def handle(request):
        if request.url.path.startswith("/auth") and failure != 403:
            return httpx.Response(200, json={"accessToken": "token"})
        if isinstance(failure, Exception):
            raise failure
        return httpx.Response(failure)

    player = PearPlayer(transport=httpx.MockTransport(handle))
    result = await player.health()
    assert result.status == status
    assert result.hint.startswith(hint)
    await player.aclose()


async def test_real_deadline_bounds_transport_and_auth():
    async def handle(request):
        await asyncio.Event().wait()

    player = PearPlayer({"auth_timeout_s": 0.01}, transport=httpx.MockTransport(handle))
    assert (await player.health()).status == "needs restart"
    player.token = "saved"
    player.data["call_timeout_s"] = 0.01
    assert (await player.health()).status == "needs restart"
    await player.aclose()


class FakePlayer:
    capabilities = frozenset({"like", "dislike"})

    def __init__(self):
        self.current = Track("current", "Current", ("Artist",))
        self.volume = 80
        self.queued = ["already"]
        self.calls = []
        self.fail_volume = False
        self.closed = False

    async def health(self):
        return Health("ok")

    async def now_playing(self):
        return self.current

    async def play_now(self, video_id):
        self.calls.append(("play", video_id))
        self.current = Track(video_id, video_id, ("New",))

    async def queue_ids(self):
        return self.queued.copy()

    async def enqueue(self, video_id):
        self.queued.append(video_id)

    async def skip(self):
        self.calls.append("skip")
        self.current = Track("next", "Next", ("Other",))

    async def pause(self):
        self.calls.append("pause")
        self.current = replace(self.current, paused=True)

    async def resume(self):
        self.calls.append("resume")
        self.current = replace(self.current, paused=False)

    async def get_volume(self):
        return self.volume

    async def set_volume(self, level):
        self.volume = level
        if self.fail_volume:
            self.fail_volume = False
            raise RuntimeError("volume failed after applying")

    async def like(self):
        self.calls.append("like")

    async def dislike(self):
        self.calls.append("dislike")

    async def aclose(self):
        self.closed = True


class FakeSearch:
    async def songs(self, query, limit):
        return [Track("bad", "Bad", ("Bad",)), Track("seed", "Seed", ("Good",), duration_s=285)]

    async def radio(self, video_id, limit):
        return [
            Track(i, i, (a,))
            for i, a in [
                ("bad", "Bad"),
                ("artist-blocked", "Banned"),
                ("already", "Good"),
                (video_id, "Good"),
                ("one", "Good"),
                ("one", "Good"),
                ("two", "Good"),
            ]
        ]


async def test_seed_top_up_and_artist_dislikes():
    player = FakePlayer()
    dislikes = Dislikes()
    dislikes.add(Track("bad", "Bad", ("Bad",)))
    dislikes.add(Track("b1", "B1", ("Banned",)))
    dislikes.add(Track("b1", "B1", ("Banned",)))
    assert "banned" not in dislikes.values()[1]
    dislikes.add(Track("b2", "B2", ("BANNED",)))
    dj = DJ(player, FakeSearch(), dislikes=dislikes, settings={"queue_top_up": 2})
    result = await dj.seed("music")
    assert result == {"playing": "Seed by Good", "duration": "4 minutes 45 seconds"}
    assert player.calls == [("play", "seed")]
    assert player.queued == ["already", "one", "two"]
    await dj.dislike()
    assert player.calls[-2:] == ["dislike", "skip"]
    assert "seed" in dislikes.values()[0]


def test_dislikes_persist_without_game(tmp_path):
    path = tmp_path / "memory.db"
    store = MemoryStore(path)
    store.dislike_music("one", ("Artist",))
    store.dislike_music("one", ("Artist",))
    assert store.music_dislikes() == ({"one"}, set())
    store.dislike_music("two", ("ARTIST",))
    store.close()
    store = MemoryStore(path)
    assert store.music_dislikes() == ({"one", "two"}, {"artist"})
    store.close()


async def test_duck_refcount_base_change_and_error_restore():
    player = FakePlayer()
    dj = DJ(player, FakeSearch())
    await dj.acquire_duck()
    await dj.acquire_duck()
    assert player.volume == 20
    await dj.release_duck()
    assert player.volume == 20
    await dj.volume(level=60)
    assert player.volume == 15
    await dj.release_duck()
    assert player.volume == 60
    player.fail_volume = True
    with pytest.raises(RuntimeError):
        await dj.acquire_duck()
    assert player.volume == 60 and dj.ducks == 0
    await dj.acquire_duck()
    await dj.aclose()
    assert player.volume == 60


async def test_busy_state_overlaps_and_remark_pause():
    player = FakePlayer()
    dj = DJ(player, FakeSearch())
    await dj.speech_state(SpeechState(listening=True, speaking=True))
    assert dj.ducks == 2 and player.volume == 20
    await dj.speech_state(SpeechState(speaking=True))
    assert player.volume == 20
    await dj.speech_state(SpeechState(speaking=True, event_kind="music_track"))
    assert player.volume == 80 and player.current.paused
    await dj.speech_state(SpeechState())
    assert not player.current.paused and player.calls == ["pause", "resume"]


@pytest.mark.parametrize("user_change", ["none", "pause", "track", "already_paused", "resume"])
async def test_critical_pause_ownership(user_change):
    player = FakePlayer()
    dj = DJ(player, FakeSearch())
    if user_change == "already_paused":
        await player.pause()
    await dj.speech_state(SpeechState(critical=True, speaking=True))
    assert player.current.paused
    actions = {"pause": dj.pause, "track": player.skip, "resume": dj.resume}
    if user_change in actions:
        await actions[user_change]()
    await dj.speech_state(SpeechState())
    assert ("resume" in player.calls) == (user_change in {"none", "resume"})


async def test_track_cooldown_and_requested_track_suppression():
    player = FakePlayer()
    now = [0]
    service = PearMusicService({}, player=player, searcher=FakeSearch(), clock=lambda: now[0])
    event = await service.poll()
    assert event.kind == "music_track" and event.data["ttl_s"] == 15
    assert await service.poll() is None
    player.current = Track("two", "Two")
    now[0] = 10
    assert await service.poll() is None
    now[0] = 1000
    player.current = Track("three", "Three")
    assert await service.poll() is not None
    await service.dj.seed("music")
    now[0] = 2000
    assert await service.poll() is None
    assert "bad" in service.describe()
    await service.aclose()
    assert player.closed


async def test_tool_schema_carrier_and_health_hint_once(caplog):
    player = FakePlayer()
    service = PearMusicService({}, player=player, searcher=FakeSearch())
    registry = ToolRegistry()
    for tool in service.service_tools():
        registry.register(tool.spec, tool.handler, carrier_only=tool.carrier_only)
    assert [spec.name for spec in registry.specs(carrier=False)] == ["now_playing"]
    assert "error" in await registry.execute(ToolCall("1", "set_music_volume", {"level": 101}))
    assert "error" in await registry.execute(ToolCall("2", "play_music", {}))
    assert "error" in await registry.execute(ToolCall("3", "set_music_volume", {}))

    async def unavailable():
        return Health("not running", "Open YouTube Music")

    player.health = unavailable
    assert await service._execute("skip_track") == {"error": "Open YouTube Music"}
    await service._execute("skip_track")
    assert caplog.text.count("Open YouTube Music") == 1


async def test_search_conversion_and_deadline():
    class Client:
        def search(self, query, **kwargs):
            return [
                {
                    "videoId": "one",
                    "title": "One",
                    "artists": [{"name": "A"}],
                    "album": {"name": "Album"},
                    "duration": "3:02",
                },
                {"title": "No ID"},
            ]

        def get_watch_playlist(self, **kwargs):
            return {"tracks": self.search("")}

    search = Searcher(Client(), timeout=1)
    tracks = await search.songs("query", 2)
    assert tracks == [Track("one", "One", ("A",), "Album", 182)]
    assert await search.radio("one", 2) == tracks


async def test_handshake_storage_reauth_and_play_order(tmp_path):
    requests = []
    auth_count = 0

    def handle(request):
        nonlocal auth_count
        requests.append(request)
        if request.url.path == "/auth/familiar":
            auth_count += 1
            return httpx.Response(200, json={"accessToken": f"token-{auth_count}"})
        if request.url.path.endswith("song-info"):
            if request.headers["Authorization"] == "Bearer token-1":
                return httpx.Response(401)
            return httpx.Response(200, json={"videoId": "song", "title": "Song", "artist": "A"})
        return httpx.Response(204)

    player = PearPlayer(transport=httpx.MockTransport(handle), state_dir=tmp_path)
    assert (await player.now_playing()).title == "Song"
    assert auth_count == 2
    assert player.token_path.read_text() == "token-2"
    assert player.token_path.stat().st_mode & 0o777 == 0o600
    requests.clear()
    await player.play_now("new")
    assert [r.url.path for r in requests] == ["/api/v1/queue", "/api/v1/next"]
    assert json.loads(requests[0].content) == {
        "videoId": "new",
        "insertPosition": "INSERT_AFTER_CURRENT_VIDEO",
    }
    await player.aclose()
    player = PearPlayer(transport=httpx.MockTransport(handle), state_dir=tmp_path)
    assert (await player.health()).status == "ok"
    assert auth_count == 2
    await player.aclose()


async def test_connect_error_and_no_endless_reauth(tmp_path):
    calls = []

    def denied(request):
        calls.append(request.url.path)
        if request.url.path.startswith("/auth/"):
            return httpx.Response(200, json={"accessToken": "token"})
        return httpx.Response(403)

    player = PearPlayer(transport=httpx.MockTransport(denied), state_dir=tmp_path)
    assert (await player.health()).status == "not authorised"
    assert calls == ["/auth/familiar", "/api/v1/song-info", "/auth/familiar", "/api/v1/song-info"]
    await player.aclose()

    def refused(request):
        raise httpx.ConnectError("refused")

    player = PearPlayer(transport=httpx.MockTransport(refused), state_dir=tmp_path)
    assert (await player.health()).status == "not running"
    await player.aclose()


async def test_poll_parks_until_explicit_health_recovery():
    player = FakePlayer()
    checks = []
    ready = asyncio.Event()

    async def unavailable():
        checks.append(1)
        ready.set()
        return Health("not running", "Open YouTube Music")

    player.health = unavailable
    service = PearMusicService({}, player=player, searcher=FakeSearch())
    feed = service.events()
    pending = asyncio.create_task(anext(feed))
    await ready.wait()
    await asyncio.sleep(0)
    assert not pending.done() and len(checks) == 1
    await service.speech_state(SpeechState(speaking=True))
    assert player.volume == 80

    async def healthy():
        return Health("ok")

    player.health = healthy
    assert "playing" in await service._execute("now_playing")
    assert (await asyncio.wait_for(pending, 1)).kind == "music_track"
    await feed.aclose()
    await service.aclose()


async def test_reseed_uses_current_radio_and_cooldown():
    player = FakePlayer()
    calls = []

    class Search(FakeSearch):
        async def radio(self, video_id, limit):
            calls.append(("radio", video_id))
            return await super().radio(video_id, limit)

        async def songs(self, query, limit):
            calls.append(("search", query))
            return await super().songs(query, limit)

    now = [0]
    dj = DJ(player, Search(), clock=lambda: now[0])
    await dj.reseed()
    assert ("radio", "current") in calls
    calls.clear()
    assert "already" in (await dj.reseed(calmer=True))["status"]
    assert not calls
    now[0] = 100
    await dj.reseed(calmer=True)
    assert ("search", dj.settings["calmer_query"]) in calls


async def test_pause_failure_and_shutdown_restore_owned_pause():
    player = FakePlayer()
    dj = DJ(player, FakeSearch())
    original_pause = player.pause

    async def fails_after_pause():
        await original_pause()
        raise RuntimeError("request failed after applying")

    player.pause = fails_after_pause
    with pytest.raises(RuntimeError):
        await dj.speech_state(SpeechState(critical=True))
    assert not player.current.paused
    player.pause = original_pause
    await dj.speech_state(SpeechState(critical=True))
    await dj.aclose()
    assert not player.current.paused


@pytest.mark.parametrize(
    "settings",
    [
        {"duck_level": 2},
        {"queue_top_up": 0},
        {"search_timeout_s": float("nan")},
        {"poll_interval_s": 0},
        {"auth_timeout_s": -1},
    ],
)
def test_invalid_settings_fail_clearly(settings):
    from familiar.interfaces import ConfigError

    with pytest.raises(ConfigError):
        PearMusicService(settings, player=FakePlayer(), searcher=FakeSearch())


async def test_pear_volume_and_queue_response_shapes(tmp_path):
    def handle(request):
        payloads = {
            "/api/v1/volume": {"state": 75},
            "/api/v1/queue": {
                "items": [
                    {"playlistPanelVideoRenderer": {"videoId": "wrapped"}},
                    {"videoId": "flat"},
                    {"unavailable": True},
                ]
            },
        }
        return httpx.Response(200, json=payloads[request.url.path])

    player = PearPlayer(transport=httpx.MockTransport(handle), state_dir=tmp_path)
    player.token = "saved"
    assert await player.get_volume() == 75
    assert await player.queue_ids() == ["wrapped", "flat"]
    await player.aclose()


async def test_failed_resume_keeps_ownership_for_shutdown():
    player = FakePlayer()
    dj = DJ(player, FakeSearch())
    await dj.speech_state(SpeechState(critical=True))
    original_resume = player.resume

    async def fails():
        raise RuntimeError("temporarily unavailable")

    player.resume = fails
    with pytest.raises(RuntimeError):
        await dj.speech_state(SpeechState())
    assert player.current.paused and dj.interrupting
    player.resume = original_resume
    await dj.aclose()
    assert not player.current.paused and not dj.interrupting


async def test_poll_recovers_by_periodic_health_check():
    player = FakePlayer()
    statuses = [Health("not running", "Open YouTube Music"), Health("ok")]

    async def health():
        return statuses.pop(0) if len(statuses) > 1 else statuses[0]

    player.health = health
    service = PearMusicService({"recovery_interval_s": 0.01}, player=player, searcher=FakeSearch())
    feed = service.events()
    assert (await asyncio.wait_for(anext(feed), 1)).kind == "music_track"
    await feed.aclose()


async def test_tool_quantities_are_spoken_and_player_values_stay_numeric():
    player = FakePlayer()
    player.current = replace(player.current, duration_s=3661)
    service = PearMusicService({}, player=player, searcher=FakeSearch())
    result = await service._execute("now_playing")
    assert result["duration"] == "1 hour 1 minute 1 second"
    result = await service._execute("set_music_volume", level=60)
    assert result["volume"] == "60 percent"
    assert player.volume == 60
    assert player.current.duration_s == 3661
    player.current = None
    result = await service._execute("now_playing")
    assert result == {"playing": "Nothing is playing", "duration": "unknown"}
    await service.aclose()
