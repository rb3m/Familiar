"""Offline music contract tests: fake HTTP, discovery, clock and playback."""

import asyncio
import json
import random
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from familiar.interfaces import SpeechState, ToolCall
from familiar.memory.store import MemoryStore
from familiar.tools import ToolRegistry
from familiar_music.dj import DJ, Dislikes
from familiar_music.pear import Health, PearPlayer
from familiar_music.player import Playlist, Track
from familiar_music.search import Searcher
from familiar_music.service import PearMusicService


@pytest.fixture(autouse=True)
def isolated_data(monkeypatch, tmp_path):
    monkeypatch.setenv("FAMILIAR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))


def queued(video_id):
    """A Pear queue response where the bare video ID has landed after the current song."""
    items = [{"videoId": "now", "selected": True}, {"videoId": video_id}]
    return httpx.Response(200, json={"items": items})


async def test_token_storage_reauth_and_play_order(tmp_path):
    calls = []
    tokens = iter(["first", "second"])
    reject = True

    def handle(request):
        nonlocal reject
        calls.append((request.method, request.url.path, request.content))
        if request.url.path == "/auth/familiar":
            return httpx.Response(200, json={"accessToken": next(tokens)})
        if request.method == "GET":
            return queued("seed")
        if request.url.path.endswith("queue") and reject:
            reject = False
            return httpx.Response(401)
        return httpx.Response(204)

    player = PearPlayer(transport=httpx.MockTransport(handle))
    await player.play_now("ytm:seed")
    assert [path for _, path, _ in calls] == [
        "/auth/familiar",
        "/api/v1/queue",
        "/auth/familiar",
        "/api/v1/queue",
        "/api/v1/queue",  # waits until Pear has added the song
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
        self.current = Track("ytm:current", "Current", ("Artist",))
        self.volume = 80
        self.queued = ["ytm:already"]
        self.calls = []
        self.fail_volume = False
        self.closed = False

    async def health(self):
        return Health("ok")

    async def now_playing(self):
        return self.current

    async def play_now(self, uri):
        self.calls.append(("play", uri))
        self.current = Track(uri, uri.removeprefix("ytm:"), ("New",))

    async def queue_ids(self):
        return self.queued.copy()

    async def enqueue(self, uri):
        self.queued.append(uri)

    async def skip(self):
        self.calls.append("skip")
        self.current = Track("ytm:next", "Next", ("Other",))

    async def pause(self):
        self.calls.append("pause")
        if self.current is not None:
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
        return [
            Track("ytm:bad", "Bad", ("Bad",)),
            Track("ytm:seed", "Seed", ("Good",), duration_s=285),
        ]

    async def playlists(self, query, limit):
        return [Playlist("pl-a", f"{query} A"), Playlist("pl-b", f"{query} B")]

    async def playlist_tracks(self, playlist_id, limit):
        return [Track(f"ytm:{playlist_id}-{i}", f"Song {i}", ("Band",)) for i in range(limit)][:30]

    async def radio(self, uri, limit):
        return [
            Track(i, i, (a,))
            for i, a in [
                ("ytm:bad", "Bad"),
                ("ytm:artist-blocked", "Banned"),
                ("ytm:already", "Good"),
                (uri, "Good"),
                ("ytm:one", "Good"),
                ("ytm:one", "Good"),
                ("ytm:two", "Good"),
            ]
        ]


async def test_seed_top_up_and_artist_dislikes():
    player = FakePlayer()
    dislikes = Dislikes()
    dislikes.add(Track("ytm:bad", "Bad", ("Bad",)))
    dislikes.add(Track("ytm:b1", "B1", ("Banned",)))
    dislikes.add(Track("ytm:b1", "B1", ("Banned",)))
    assert "banned" not in dislikes.values()[1]
    dislikes.add(Track("ytm:b2", "B2", ("BANNED",)))
    dj = DJ(player, FakeSearch(), dislikes=dislikes, settings={"queue_top_up": 2})
    result = await dj.seed("music")
    assert result == {
        "playing": "Seed by Good",
        "source": "YouTube Music",
        "duration": "4 minutes 45 seconds",
        "volume": "70 percent",
    }
    assert player.calls == [("play", "ytm:seed")]
    assert player.queued == ["ytm:already", "ytm:one", "ytm:two"]
    await dj.dislike()
    assert player.calls[-2:] == ["dislike", "skip"]
    assert "ytm:seed" in dislikes.values()[0]


def test_dislikes_persist_without_game(tmp_path):
    path = tmp_path / "memory.db"
    store = MemoryStore(path)
    store.dislike_music("ytm:one", ("Artist",))
    store.dislike_music("ytm:one", ("Artist",))
    assert store.music_dislikes() == ({"ytm:one"}, set())
    store.dislike_music("ytm:two", ("ARTIST",))
    store.close()
    store = MemoryStore(path)
    assert store.music_dislikes() == ({"ytm:one", "ytm:two"}, {"artist"})
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


class FixedRng:
    """randint returns the low bound, so cadences are predictable."""

    def randint(self, low, high):
        return low

    def shuffle(self, items):
        pass


async def change(service, player, uri):
    player.current = Track(uri, uri.removeprefix("ytm:").title(), ("Band",))
    return await service.poll()


async def test_remark_every_few_songs_and_only_once_heard():
    player = FakePlayer()
    service = PearMusicService(
        {"remark_every": [3, 4], "autonomous_picks": False},
        player=player,
        searcher=FakeSearch(),
        rng=FixedRng(),
    )
    assert await service.poll() is None
    assert await service.poll() is None  # same song: not a change
    assert await change(service, player, "ytm:two") is None
    event = await change(service, player, "ytm:three")
    assert event.kind == "music_track" and event.data["cue"] and event.data["ttl_s"] == 15
    assert event.data["uri"] == player.current.uri
    assert "video_id" not in event.data
    # Dropped by the Director (never acknowledged): the next change tries again.
    assert (await change(service, player, "ytm:four")).kind == "music_track"
    await service.speech_state(SpeechState(speaking=True, event_kind="music_track"))
    assert await change(service, player, "ytm:five") is None
    await service.dj.seed("music")
    assert await service.poll() is None  # a requested song is not counted
    assert "bad" in service.describe()
    await service.aclose()
    assert player.closed


async def test_dj_turn_goes_to_the_carrier_with_play_music_only():
    player = FakePlayer()
    now = [0]
    service = PearMusicService(
        {"remark_every": [9, 9], "dj_turn_every": [2, 2], "autonomy_hold_s": 100},
        player=player,
        searcher=FakeSearch(),
        clock=lambda: now[0],
        rng=FixedRng(),
    )
    await service.poll()
    event = await change(service, player, "ytm:two")
    assert event.kind == "music_dj_turn"
    assert event.data["speaker"] == "carrier" and event.data["tools"] == ["play_music"]
    assert event.data["taste"] == "music" and event.data["cue"] and "Two" in event.data["summary"]
    await service.speech_state(SpeechState(speaking=True, event_kind="music_dj_turn"))
    assert service.picks.count == 0 and service.remarks.count == 0
    tools = {tool.spec.name: tool for tool in service.service_tools()}
    assert [name for name, tool in tools.items() if tool.autonomous] == ["play_music"]
    await service.aclose()


async def test_user_requests_and_pause_hold_back_autonomous_picks():
    player = FakePlayer()
    now = [0]
    service = PearMusicService(
        {"remark_every": [9, 9], "dj_turn_every": [1, 1], "autonomy_hold_s": 100},
        player=player,
        searcher=FakeSearch(),
        clock=lambda: now[0],
        rng=FixedRng(),
    )
    await service._execute("skip_track")
    assert await change(service, player, "ytm:two") is None  # inside the hold
    now[0] = 200
    await service._execute("pause_music")
    assert await change(service, player, "ytm:three") is None  # paused: no picks
    await service._execute("resume_music")
    assert (await change(service, player, "ytm:four")).kind == "music_dj_turn"
    await service._execute("set_music_volume", level=50)  # volume is not a selection
    assert (await change(service, player, "ytm:five")).kind == "music_dj_turn"
    await service.aclose()


@pytest.mark.parametrize(
    "settings",
    [
        {"remark_every": [4, 3]},
        {"remark_every": [0, 2]},
        {"dj_turn_every": 5},
        {"autonomous_picks": "yes"},
        {"autonomy_hold_s": -1},
    ],
)
def test_invalid_cadence_settings(settings):
    from familiar.interfaces import ConfigError

    with pytest.raises(ConfigError):
        PearMusicService(settings, player=FakePlayer(), searcher=FakeSearch())


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
            assert kwargs == {"videoId": "one", "limit": 2}
            return {"tracks": self.search("")}

    search = Searcher(Client(), timeout=1)
    tracks = await search.songs("query", 2)
    assert tracks == [Track("ytm:one", "One", ("A",), "Album", 182)]
    assert await search.radio("ytm:one", 2) == tracks


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
        if request.method == "GET":
            return queued("new")
        return httpx.Response(204)

    player = PearPlayer(transport=httpx.MockTransport(handle), state_dir=tmp_path)
    track = await player.now_playing()
    assert track.title == "Song" and track.uri == "ytm:song"
    assert auth_count == 2
    assert player.token_path.read_text() == "token-2"
    assert player.token_path.stat().st_mode & 0o777 == 0o600
    requests.clear()
    await player.play_now("ytm:new")
    assert [r.url.path for r in requests] == ["/api/v1/queue", "/api/v1/queue", "/api/v1/next"]
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
    service = PearMusicService({"remark_every": [1, 1]}, player=player, searcher=FakeSearch())
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
        async def radio(self, uri, limit):
            calls.append(("radio", uri))
            return await super().radio(uri, limit)

        async def songs(self, query, limit):
            calls.append(("search", query))
            return await super().songs(query, limit)

    now = [0]
    dj = DJ(player, Search(), clock=lambda: now[0], rng=FixedRng())
    await dj.reseed()
    assert ("radio", "ytm:current") in calls
    calls.clear()
    assert "already" in (await dj.reseed(calmer=True))["status"]
    assert not calls
    now[0] = 100
    result = await dj.reseed(calmer=True)
    assert result["playlist"] == f"{dj.settings['calmer_query']} A"


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
        {"volume_tolerance": -1},
        {"play_volume": 101},
        {"calm_volume": -5},
        {"volume_curve": [[0, 0], [50, 60], [100, 40]]},
        {"volume_curve": [[0, 0]]},
        {"volume_curve": "flat"},
    ],
)
def test_invalid_settings_fail_clearly(settings):
    from familiar.interfaces import ConfigError

    with pytest.raises(ConfigError):
        PearMusicService(settings, player=FakePlayer(), searcher=FakeSearch())


async def test_pear_volume_and_queue_response_shapes(tmp_path):
    def handle(request):
        payloads = {
            "/api/v1/volume": {"state": 40},
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
    assert await player.get_volume() == 70  # read on Pear's curve, reported as written
    assert await player.queue_ids() == ["ytm:wrapped", "ytm:flat"]
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
    service = PearMusicService(
        {"recovery_interval_s": 0.01, "remark_every": [1, 1]}, player=player, searcher=FakeSearch()
    )
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


class LaggingPlayer(FakePlayer):
    """Pear-like: a read right after a write still returns the previous level, rounded."""

    def __init__(self, volume=70):
        super().__init__()
        self.volume = self.audible = volume

    async def get_volume(self):
        return round(self.audible)

    async def set_volume(self, level):
        self.audible, self.volume = self.volume, level


async def test_repeated_ducks_restore_full_volume_despite_lagging_reads():
    player = LaggingPlayer()
    dj = DJ(player, FakeSearch())
    for _ in range(5):
        await dj.speech_state(SpeechState(listening=True))
        await dj.speech_state(SpeechState())
        await dj.speech_state(SpeechState(speaking=True))
        await dj.speech_state(SpeechState())
    assert player.volume == 70


async def test_manual_volume_change_between_ducks_is_kept():
    player = FakePlayer()
    dj = DJ(player, FakeSearch())
    await dj.speech_state(SpeechState(listening=True))
    await dj.speech_state(SpeechState())
    player.volume = 40
    await dj.speech_state(SpeechState(speaking=True))
    await dj.speech_state(SpeechState())
    assert player.volume == 40


def test_pear_volume_curve_inverts_reads():
    from familiar_music.pear import _written_level

    curve = [(0.0, 0.0), (50.0, 20.0), (100.0, 100.0)]
    assert _written_level(0, curve) == 0
    assert _written_level(10, curve) == 25
    assert _written_level(60, curve) == 75
    assert _written_level(120, curve) == 100


async def test_play_and_calm_volume_defaults():
    player = FakePlayer()
    dj = DJ(player, FakeSearch())
    result = await dj.seed("disco")
    assert player.volume == 70 and result["volume"] == "70 percent"
    dj.last_seed = float("-inf")  # past the reseed cooldown
    await dj.reseed(calmer=True)
    assert player.volume == 40
    await dj.volume(level=55)
    await dj.seed("disco")
    assert player.volume == 55
    dj.last_seed = float("-inf")
    await dj.reseed(calmer=True)
    assert player.volume == 40


class ClearingPlayer(FakePlayer):
    capabilities = frozenset({"like", "dislike", "clear"})

    async def clear_upcoming(self):
        self.calls.append("clear")
        self.queued = []


async def test_genre_replaces_queue_with_shuffled_playlist_batch():
    player = ClearingPlayer()
    dj = DJ(player, FakeSearch(), settings={"genre_batch": 4, "queue_top_up": 3}, rng=FixedRng())
    result = await dj.seed("70s disco classics", kind="genre")
    assert player.calls[:2] == ["clear", ("play", player.current.uri)]
    assert result["playlist"] == "70s disco classics A" and result["queued"] == "3 more songs"
    assert len(player.queued) == 3 and "ytm:already" not in player.queued
    assert all(v.startswith("ytm:pl-a-") for v in player.queued + [player.current.uri])
    reserve = len(dj.reserve)
    player.queued = player.queued[:1]
    await dj.top_up()
    assert len(player.queued) == 3 and len(dj.reserve) == reserve - 2


async def test_asking_again_picks_something_new():
    player = ClearingPlayer()
    dj = DJ(player, FakeSearch(), rng=FixedRng())

    class TwoSongs(FakeSearch):
        async def songs(self, query, limit):
            return [Track("ytm:top", "Top", ("A",)), Track("ytm:second", "Second", ("A",))]

    dj.sources["ytm"].searcher = TwoSongs()
    assert (await dj.seed("classic rock", kind="song"))["playing"] == "Top by A"
    assert (await dj.seed("classic rock", kind="song"))["playing"] == "Second by A"
    assert (await dj.seed("disco", kind="genre"))["playlist"] == "disco A"
    assert (await dj.seed("disco", kind="genre"))["playlist"] == "disco B"


async def test_pear_upcoming_queue_and_clear(tmp_path):
    def item(video, selected=False):
        return {"playlistPanelVideoRenderer": {"videoId": video, "selected": selected}}

    queue = [
        item("played"),
        item("now", selected=True),
        {"playlistPanelVideoWrapperRenderer": {"primaryRenderer": item("wrapped")}},
        item("last"),
    ]
    deleted = []

    def handle(request):
        if request.method == "DELETE":
            deleted.append(int(request.url.path.rsplit("/", 1)[1]))
            return httpx.Response(204)
        return httpx.Response(200, json={"items": queue})

    player = PearPlayer(transport=httpx.MockTransport(handle), state_dir=tmp_path)
    player.token = "saved"
    assert await player.queue_ids() == ["ytm:wrapped", "ytm:last"]
    await player.clear_upcoming()
    assert deleted == [3, 2]
    await player.aclose()


async def test_play_now_waits_for_pear_to_add_the_song(tmp_path):
    landed = iter([False, False, True])
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return queued({True: "new", False: "other"}[next(landed)])
        return httpx.Response(204)

    player = PearPlayer({"insert_poll_s": 0.001}, transport=httpx.MockTransport(handle))
    player.token = "saved"
    await player.play_now("ytm:new")
    assert [c for c in calls if c[0] == "GET"] == [("GET", "/api/v1/queue")] * 3
    assert calls[-1] == ("POST", "/api/v1/next")
    await player.aclose()


class StableRadio(FakeSearch):
    """Radio that returns the same numbered list per seed, like a real recommendation list."""

    async def radio(self, uri, limit):
        return [Track(f"{uri}>{i}", str(i), ("Band",)) for i in range(limit)]


async def test_radio_keeps_replenishing_with_stable_results():
    player = FakePlayer()
    player.queued = []
    dj = DJ(player, StableRadio())
    dj.radio_seed = "ytm:seed"
    await dj.top_up()
    played = 0
    while player.queued and played < 200:
        player.queued.pop(0)
        played += 1
        await dj.top_up()
    assert played == 200


async def test_unusable_reserve_falls_through_to_radio():
    player = FakePlayer()
    player.queued = []
    dj = DJ(player, StableRadio(), settings={"queue_top_up": 2})
    blocked = Track("ytm:blocked", "Blocked", ("Nope",))
    dj.dislikes.add(blocked)
    dj.reserve = [blocked]
    dj.radio_seed = "ytm:seed"
    await dj.top_up()
    assert player.queued == ["ytm:seed>0", "ytm:seed>1"] and dj.reserve == []


async def test_more_like_this_drops_the_genre_reserve():
    player = ClearingPlayer()
    dj = DJ(player, StableRadio(), settings={"genre_batch": 2})
    await dj.seed("disco", kind="genre")
    assert dj.reserve
    dj.last_seed = float("-inf")
    await dj.reseed()
    assert dj.reserve == [] and any(v.startswith(f"{player.current.uri}>") for v in player.queued)


async def test_playlist_choice_varies_only_among_top_results():
    class Many(FakeSearch):
        async def playlists(self, query, limit):
            return [Playlist(f"pl-{i}", str(i)) for i in range(6)]

    chosen = set()
    for seed in range(30):
        dj = DJ(ClearingPlayer(), Many(), rng=random.Random(seed))
        chosen.add((await dj.seed("disco", kind="genre"))["playlist"])
    assert chosen == {"0", "1", "2"}


def dj_service(player, now, **settings):
    return PearMusicService(
        {"remark_every": [9, 9], "dj_turn_every": [1, 1], "autonomy_hold_s": 100, **settings},
        player=player,
        searcher=FakeSearch(),
        clock=lambda: now[0],
        rng=FixedRng(),
    )


async def test_queued_dj_turn_cannot_undo_a_later_request():
    player = FakePlayer()
    now = [0]
    service = dj_service(player, now)
    assert (await change(service, player, "ytm:two")).kind == "music_dj_turn"
    await service._execute("pause_music")  # the Commander spoke first
    await service.speech_state(SpeechState(speaking=True, event_kind="music_dj_turn"))
    result = await service._execute("play_music", query="disco", kind="genre")
    assert "error" in result and player.current.paused
    await service.aclose()


async def test_externally_paused_player_gets_no_dj_turn():
    player = FakePlayer()
    service = dj_service(player, [0])
    player.current = Track("ytm:two", "Two", paused=True)
    assert await service.poll() is None
    await service.aclose()


async def test_skips_and_dislikes_are_not_natural_changes():
    player = FakePlayer()
    service = dj_service(player, [0], dj_turn_every=[9, 9], remark_every=[1, 1])
    await service._execute("skip_track")
    assert await service.poll() is None  # the skipped-to song
    assert (await change(service, player, "ytm:natural")).kind == "music_track"
    await service.aclose()


async def test_one_turn_is_acknowledged_once():
    player = FakePlayer()
    service = dj_service(player, [0], dj_turn_every=[9, 9], remark_every=[3, 3])
    speaking = SpeechState(speaking=True, event_kind="music_track")
    await service.speech_state(speaking)
    await change(service, player, "ytm:two")
    await service.speech_state(SpeechState(listening=True, speaking=True, event_kind="music_track"))
    assert service.remarks.count == 1  # push-to-talk mid-turn did not restart the count
    await service.speech_state(SpeechState())
    await service.speech_state(speaking)
    assert service.remarks.count == 0  # a new turn does
    await service.aclose()


@pytest.mark.parametrize("method", ["play_now", "enqueue"])
@pytest.mark.parametrize("uri", ["local:track-uuid", "bare-video-id"])
async def test_pear_rejects_unsupported_uris(method, uri, tmp_path):
    def unexpected_request(request):
        pytest.fail("Unsupported URIs must be rejected before contacting Pear")

    player = PearPlayer(transport=httpx.MockTransport(unexpected_request), state_dir=tmp_path)
    with pytest.raises(ValueError, match="Pear only supports ytm:"):
        await getattr(player, method)(uri)
    await player.aclose()


async def test_pear_enqueue_sends_bare_video_id(tmp_path):
    payloads = []

    def handle(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(204)

    player = PearPlayer(transport=httpx.MockTransport(handle), state_dir=tmp_path)
    player.token = "saved"
    await player.enqueue("ytm:queued-song")
    assert payloads == [{"videoId": "queued-song", "insertPosition": "INSERT_AT_END"}]
    await player.aclose()


@pytest.fixture
def library(tmp_path):
    from familiar_music.library.index import LibraryIndex

    index = LibraryIndex(tmp_path / "library.sqlite")
    with index._connect() as conn:
        conn.executemany(
            "INSERT INTO tracks "
            "(uuid, root, relpath, size, mtime, header_hash, title, artist, artist_norm, "
            "search_text, album, year, genre, duration_s, codec, lossless) "
            "VALUES (?, ?, ?, 1, 0, '', ?, ?, ?, ?, 'Album', 1985, 'New Wave', 180, 'mp3', 0)",
            [
                (
                    str(i),
                    str(tmp_path),
                    f"{i}.mp3",
                    f"Song {i}",
                    f"Band {i % 3}",
                    f"band {i % 3}",
                    f"band {i % 3} song {i}",
                )
                for i in range(18)
            ],
        )
        conn.executemany(
            "INSERT INTO track_genres VALUES (?, 'newwave')", [(str(i),) for i in range(18)]
        )
        conn.execute("INSERT INTO tag_display VALUES ('newwave', 'new wave')")
    return index


class LocalPlayer(ClearingPlayer):
    capabilities = frozenset({"clear", "volume"})

    def __init__(self, index):
        super().__init__()
        self.index = index
        self.current = None
        self.queued = []

    async def play_now(self, uri):
        self.calls.append(("play", uri))
        self.current = self.index.resolve(uri)[1]

    async def pause(self):
        self.calls.append("pause")
        if self.current is not None:
            self.current = replace(self.current, paused=True)

    async def skip(self):
        self.current = None
        if self.queued:
            await self.play_now(self.queued.pop(0))


def blend_dj(library, **settings):
    from familiar_music.sourced import SourcedPlayer

    pear, local = ClearingPlayer(), LocalPlayer(library)
    player = SourcedPlayer({"ytm": pear, "local": local})
    dj = DJ(player, FakeSearch(), index=library, settings=settings, rng=random.Random(3))
    return dj, pear, local


async def test_sourced_switches_pauses_routes_and_rejects_mismatched_enqueue(library):
    dj, pear, local = blend_dj(library)
    player = dj.player
    await player.set_volume(37)
    await player.play_now("local:0")
    assert "pause" in pear.calls and player.active == "local"
    assert local.volume == 37 and player.capabilities == local.capabilities
    with pytest.raises(ValueError, match="active source"):
        await player.enqueue("ytm:wrong")
    await player.enqueue("local:1")
    assert await player.queue_ids() == ["local:1"]
    await player.clear_upcoming()
    await player.pause()
    assert (await player.now_playing()).paused
    await player.resume()
    assert not (await player.now_playing()).paused
    await player.play_now("ytm:next")
    assert local.current.paused and player.active == "ytm" and pear.volume == 37
    await player.like()
    await player.dislike()
    assert pear.calls[-2:] == ["like", "dislike"]
    await player.aclose()
    assert pear.closed and local.closed


@pytest.mark.parametrize("ducked", [False, True])
async def test_blend_carries_logical_volume_across_switches(library, ducked):
    dj, pear, local = blend_dj(library)
    await dj.volume(level=52)
    await dj.speech_state(SpeechState(listening=ducked))
    result = await dj.seed("Song 1", source="library")
    assert result["source"] == "library"
    assert local.volume == 52 * (0.25 if ducked else 1)
    assert dj.level == 52
    await dj.speech_state(SpeechState())
    assert local.volume == 52
    await dj.seed("something", source="youtube")
    assert pear.volume == 52


async def test_blend_switch_preserves_speech_pause_owner(library):
    dj, _, local = blend_dj(library)
    await dj.speech_state(SpeechState(critical=True))
    await dj.seed("Song 1", source="library")
    assert local.current.paused and dj.interrupting
    await dj.speech_state(SpeechState())
    assert not local.current.paused and not dj.interrupting


async def test_blend_source_forcing_and_local_first_requests(library):
    dj, _, _ = blend_dj(library)
    assert (await dj.seed("Song 1"))["source"] == "library"
    assert "error" in await dj.seed("absent", source="library")
    assert (await dj.seed("absent"))["source"] == "YouTube Music"
    assert (await dj.seed("Band 2", kind="artist"))["source"] == "library"
    assert (await dj.seed("Song 2", source="youtube"))["source"] == "YouTube Music"


async def test_local_genre_spacing_filters_and_minimum_fallback(library):
    dj, _, _ = blend_dj(
        library, genre_batch=8, blend={"library": 100, "persona": 0, "discovery": 0}
    )
    source = dj.sources["local"]
    selection = await source.genre("80s new wave")
    tracks = selection.tracks + selection.reserve
    assert len(selection.tracks) == 8 and len(tracks) == 18
    assert all(a.artists != b.artists for a, b in zip(tracks, tracks[1:], strict=False))
    assert all(t.year == 1985 and t.genre == "New Wave" for t in tracks)
    assert await source.genre("unrecognised") is None
    assert await source.genre("new wave", years=[1990, 1999]) is None
    assert await source.genre("words", genres=["new wave"], years=[1980, 1989])
    dj.settings["local_min_matches"] = 19
    assert (await dj.seed("80s new wave", kind="genre"))["source"] == "YouTube Music"
    assert (await dj.seed("80s new wave", kind="genre", source="library"))["source"] == "library"


async def test_local_radio_excludes_recent_dislikes_and_explicit_exclusions(library):
    dj, _, _ = blend_dj(library)
    dj.recent.append("local:1")
    dj.dislikes.add(library.resolve("local:2")[1])
    selection = await dj.sources["local"].radio("local:0", 30, {"local:3"})
    assert selection is not None
    uris = {t.uri for t in selection.tracks}
    assert uris.isdisjoint({"local:0", "local:1", "local:2", "local:3"})
    assert len(uris) == 14


@pytest.mark.parametrize("down,expected", [("ytm", "library"), ("local", "YouTube Music")])
async def test_one_backend_down_does_not_block_other_source(library, down, expected):
    dj, pear, local = blend_dj(
        library, local_min_artists=3, blend={"library": 0, "persona": 0, "discovery": 1}
    )
    backend = {"ytm": pear, "local": local}[down]

    async def unavailable():
        return Health("unavailable", "closed")

    async def pause_fails():
        raise ConnectionError("closed")

    backend.health = unavailable
    backend.pause = pause_fails
    assert (await dj.player.health()).status == "ok"
    assert not dj.player.available(down)
    result = await dj.seed("80s new wave", kind="genre")
    assert result["source"] == expected
    forced = {"ytm": "youtube", "local": "library"}[down]
    assert "error" in await dj.seed("anything", source=forced)


def test_blend_lane_draw_uses_seeded_weights(library):
    dj, _, _ = blend_dj(library)
    dj.rng = random.Random(17)
    expected_rng = random.Random(17)
    weights = dj.settings["blend"]
    expected = expected_rng.choices(list(weights), weights=list(weights.values()), k=30)
    assert [dj.draw_lane() for _ in range(30)] == expected


async def test_radio_changes_lane_only_after_batch_finishes(library):
    dj, pear, local = blend_dj(
        library, genre_batch=2, queue_top_up=1, blend={"library": 1, "persona": 0, "discovery": 0}
    )
    await dj.player.play_now("ytm:seed")
    pear.queued.clear()
    dj.radio_seed = "ytm:seed"
    await dj.top_up()
    assert dj._radio_count == 1
    pear.queued.clear()
    await dj.top_up()
    assert dj._radio_count == 2
    pear.queued.clear()
    dj._radio_track = Track("ytm:last", "Last", ("Band 0",))
    await dj.top_up()
    assert dj.player.active == "ytm"  # the last radio song is still playing
    pear.current = None
    dj._radio_track = Track("ytm:last", "Last", ("Band 0",))
    await dj.top_up()
    assert dj.player.active == "local" and local.current is not None


async def test_local_remark_facts_and_tool_vocabulary(library):
    from familiar_music.service import MusicService

    dj, _, local = blend_dj(library)
    service = MusicService(
        {"remark_every": [1, 1]}, player=dj.player, searcher=FakeSearch(), index=library
    )
    await dj.player.play_now("local:0")
    event = await service.poll()
    assert event.data["summary"] == "Now playing: Song 0 by Band 0, from Album (1985)."
    result = await service._execute("now_playing")
    assert result["album"] == "Album" and result["year"] == 1985
    tool = next(t for t in service.service_tools() if t.spec.name == "play_music")
    assert "new wave" in tool.spec.description
    # ToolSpec's schema is exercised through the registry below as well as by service execution.
    registry = ToolRegistry()
    registry.register(tool.spec, tool.handler)
    result = await registry.execute(
        ToolCall(
            "pick",
            "play_music",
            {
                "query": "80s new wave",
                "kind": "genre",
                "source": "library",
                "genres": ["new wave"],
                "years": [1980, 1989],
            },
        ),
    )
    assert "error" not in str(result)
    local.current = replace(local.current, album="", year=None)
    service._current = local.current
    assert service._remark_summary() == local.current.summary()
    await service.aclose()


async def test_resolver_includes_nonpreferred_duplicates_but_not_missing(library):
    with library._connect() as conn:
        conn.execute("UPDATE tracks SET dup_group = '1' WHERE uuid = '0'")
    assert library.by_uuid("0") is None
    path, track = library.resolve("local:0")
    assert path.name == "0.mp3" and track.album == "Album" and track.year == 1985
    with library._connect() as conn:
        conn.execute("UPDATE tracks SET missing = 1 WHERE uuid = '0'")
    assert library.resolve("local:0") is None


@pytest.mark.parametrize(
    "settings",
    [
        {"blend": {"library": 0, "persona": 0, "discovery": 0}},
        {"blend": {"library": -1, "persona": 2, "discovery": 0}},
        {"blend": {"library": 1.5, "persona": 2, "discovery": 0}},
        {"blend": {"library": True, "persona": 2, "discovery": 0}},
        {"blend": {"library": 1}},
        {"blend": []},
        {"local_min_matches": 0},
        {"local_min_matches": 1.5},
        {"library_enrich": "false"},
    ],
)
def test_invalid_blend_settings(settings):
    from familiar.interfaces import ConfigError
    from familiar_music.settings import music_settings

    with pytest.raises(ConfigError):
        music_settings(settings)


@pytest.mark.parametrize("enrich", [True, False])
async def test_library_startup_worker_does_not_block_tools(library, monkeypatch, enrich):
    from threading import Event

    import familiar_music.service as module

    started, release, finished = Event(), Event(), Event()
    calls = []

    class Scanner:
        def __init__(self, index):
            assert index is library

        def scan(self, roots):
            calls.append("scan")
            started.set()
            release.wait(3)
            finished.set()

    class Enricher:
        def __init__(self, index):
            assert index is library
            self.client = self

        def enrich_all(self, **kwargs):
            calls.append("enrich")

        def close(self):
            calls.append("close")

    monkeypatch.setattr(module, "Scanner", Scanner)
    monkeypatch.setattr(module, "Enricher", Enricher)
    service = module.MusicService(
        {"library_enrich": enrich, "poll_interval_s": 0.001, "remark_every": [1, 1]},
        player=FakePlayer(),
        searcher=FakeSearch(),
        index=library,
    )
    feed = service.events()
    await anext(feed)
    assert await asyncio.to_thread(started.wait, 1)
    result = await asyncio.wait_for(service._execute("now_playing"), 1)
    assert "playing" in result and not finished.is_set()
    release.set()
    await service._library_task
    assert calls == (["scan", "enrich", "close"] if enrich else ["scan"])
    await feed.aclose()
    await service.aclose()


async def test_library_refresh_failure_logged_once(library, monkeypatch, caplog):
    import familiar_music.service as module

    class Scanner:
        def __init__(self, index):
            pass

        def scan(self, roots):
            raise OSError("scan failed")

    monkeypatch.setattr(module, "Scanner", Scanner)
    service = module.MusicService(
        {"remark_every": [1, 1]}, player=FakePlayer(), searcher=FakeSearch(), index=library
    )
    feed = service.events()
    await anext(feed)
    await service._library_task
    assert caplog.text.count("Music library refresh failed") == 1
    assert "playing" in await service._execute("now_playing")
    await feed.aclose()
    await service.aclose()


async def test_empty_library_keeps_youtube_behavior(tmp_path):
    from familiar_music.library.index import LibraryIndex
    from familiar_music.service import MusicService

    index = LibraryIndex(tmp_path / "empty.sqlite")
    service = MusicService({}, player=FakePlayer(), searcher=FakeSearch(), index=index)
    result = await service._execute("play_music", query="disco", kind="genre")
    assert result["source"] == "YouTube Music"
    tool = next(t for t in service.service_tools() if t.spec.name == "play_music")
    assert "Library tags" not in tool.spec.description
    await service.aclose()


async def test_radio_starts_next_batch_on_same_source_after_idle(library):
    dj, _, local = blend_dj(
        library, genre_batch=2, queue_top_up=1, blend={"library": 1, "persona": 0, "discovery": 0}
    )
    await dj.seed("Song 1", source="library")
    dj._radio_count = 2
    local.current = None
    local.queued.clear()
    await dj.top_up()
    assert local.current is not None and dj.player.active == "local"
    assert dj._radio_count == 2 and len(local.queued) == 1


@pytest.mark.parametrize("uri", ["local:0", "ytm:external"])
@pytest.mark.parametrize(
    "seconds,tool,expected",
    [
        (10, "skip_track", "skipped"),
        (90, "skip_track", None),  # a late skip is not a judgement on the song
        (160, "skip_track", "played"),  # nearly finished: counts as played through
        (160, "dislike_track", "skipped"),
        (20, None, "skipped"),
        (150, None, "played"),
        (180, None, "played"),
        (90, None, None),
        (10, "play_music", None),
        (10, "calmer_music", None),
    ],
)
async def test_behaviour_classifies_previous_track(library, uri, seconds, tool, expected):
    player, now = FakePlayer(), [0]
    player.current = Track(uri, "Original", ("Band 0",), duration_s=180)
    service = PearMusicService(
        {}, player=player, searcher=FakeSearch(), index=library, clock=lambda: now[0]
    )
    await service.poll()
    now[0] = seconds
    arguments = {"play_music": {"query": "something", "source": "youtube"}}.get(tool, {})
    if tool is not None:
        assert "error" not in await service._execute(tool, **arguments)
    if tool is None:
        player.current = Track("ytm:next", "Next")
        await service.poll()
    await service.aclose()
    with library._connect() as conn:
        rows = conn.execute("SELECT * FROM plays WHERE uri = ?", (uri,)).fetchall()
    assert [r["outcome"] for r in rows] == list(filter(None, [expected]))
    if expected:
        assert rows[0]["listened_s"] == seconds
        assert datetime.fromisoformat(rows[0]["at"]).utcoffset().total_seconds() == 0


async def test_behaviour_excludes_paused_time(library):
    player, now = FakePlayer(), [0]
    player.current = Track("ytm:original", "Original", duration_s=100)
    service = PearMusicService(
        {}, player=player, searcher=FakeSearch(), index=library, clock=lambda: now[0]
    )
    await service.poll()
    now[0] = 10
    await service._execute("pause_music")
    now[0] = 210
    await service._execute("resume_music")
    now[0] = 220
    player.current = Track("ytm:next", "Next")
    await service.poll()
    await service.aclose()
    with library._connect() as conn:
        row = conn.execute("SELECT * FROM plays").fetchone()
    assert row["outcome"] == "skipped" and row["listened_s"] == 20


async def test_behaviour_ignores_remark_interruption_and_short_track(library):
    player, now = FakePlayer(), [0]
    player.current = Track("ytm:intro", "Intro", duration_s=20)
    service = PearMusicService(
        {}, player=player, searcher=FakeSearch(), index=library, clock=lambda: now[0]
    )
    await service.poll()
    now[0] = 5
    await service.speech_state(SpeechState(speaking=True, event_kind="music_track"))
    now[0] = 50
    await service.speech_state(SpeechState())
    now[0] = 65
    player.current = Track("ytm:next", "Next")
    await service.poll()
    await service.aclose()
    assert library.play_stats()["plays_recorded"] == 0


async def test_behaviour_records_short_song_that_ends_naturally(library):
    player, now = FakePlayer(), [0]
    player.current = Track("ytm:intro", "Intro", duration_s=20)
    service = PearMusicService(
        {}, player=player, searcher=FakeSearch(), index=library, clock=lambda: now[0]
    )
    await service.poll()
    now[0] = 20
    player.current = None
    await service.poll()
    await service.aclose()
    with library._connect() as conn:
        assert conn.execute("SELECT outcome FROM plays").fetchone()[0] == "played"


async def test_behaviour_write_is_off_loop_and_does_not_fail_tool(library, monkeypatch):
    import threading

    started, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()

    def blocked_write(*args):
        assert threading.get_ident() != loop_thread
        started.set()
        release.wait(2)
        raise OSError("read only")

    monkeypatch.setattr(library, "record_play", blocked_write)
    player = FakePlayer()
    service = PearMusicService({}, player=player, searcher=FakeSearch(), index=library)
    await service.poll()
    assert "error" not in await asyncio.wait_for(service._execute("skip_track"), 1)
    assert await asyncio.to_thread(started.wait, 1)
    assert "playing" in await asyncio.wait_for(service._execute("now_playing"), 1)
    release.set()
    await service.aclose()


async def test_local_weighting_seeded_and_artist_signals_decay(library):
    from datetime import timedelta

    from familiar_music.sources import artist_key

    now = datetime.now(UTC)
    liked = Track("ytm:liked", "Liked", ("Band 0",))
    skipped = Track("ytm:skipped", "Skipped", ("Band 1",))
    for _ in range(20):
        library.record_play(liked, now.isoformat(), "played", 180)
        library.record_play(skipped, now.isoformat(), "skipped", 5)
    _, _, scores = library.selection_facts(60, now + timedelta(days=60))
    assert scores["band 0", "played"] == pytest.approx(10)
    dj, _, _ = blend_dj(library, genre_batch=6)
    source = dj.sources["local"]
    source.rng = random.Random(21)
    tracks = source._tracks(library.filter())
    picks = [(await source._batch(tracks, size=1, reserve=False)).tracks[0] for _ in range(120)]
    counts = Counter(artist_key(t) for t in picks)
    assert counts["band 0"] > counts["band 2"] > counts["band 1"] > 0
    source.rng = random.Random(21)
    assert [t.uri for t in picks] == [
        (await source._batch(tracks, size=1, reserve=False)).tracks[0].uri for _ in range(120)
    ]


async def test_local_cooldown_batch_share_occasional_and_minimum_duration(library):
    from familiar_music.sources import artist_key

    with library._connect() as conn:
        conn.execute("UPDATE tracks SET duration_s = 27 WHERE uuid = '0'")
        conn.execute("UPDATE tracks SET artist = 'Short', artist_norm = 'short' WHERE uuid = '0'")
        conn.execute("INSERT INTO artist_tags VALUES ('band 1', 'salsa', 1)")
    dj, _, _ = blend_dj(library, genre_batch=8)
    source = dj.sources["local"]
    selection = await source.genre("80s new wave")
    assert all(t.duration_s >= 90 for t in selection.tracks + selection.reserve)
    assert (await source.song("Song 0")).tracks[0].duration_s == 27
    assert (await source.artist("Short")).tracks[0].duration_s == 27
    assert sum(t.artists == ("Band 1",) for t in selection.tracks) <= 1
    assert max(Counter(artist_key(t) for t in selection.tracks).values()) <= 3
    assert all(
        a.artists != b.artists for a, b in zip(selection.tracks, selection.tracks[1:], strict=False)
    )
    radio = await source.radio("local:3", 10, set())
    assert all(t.duration_s >= 90 for t in radio.tracks)
    assert max(Counter(artist_key(t) for t in radio.tracks).values()) <= 3
    source.rng = random.Random(2)
    normal = [(await source.genre("new wave")).tracks[0] for _ in range(60)]
    dj.recent_artists.append("band 0")
    source.rng = random.Random(2)
    cooled = [(await source.genre("new wave")).tracks[0] for _ in range(60)]
    assert sum(t.artists == ("Band 0",) for t in cooled) < sum(
        t.artists == ("Band 0",) for t in normal
    )


@pytest.mark.parametrize(
    "taste,minimum,prefix",
    [(["80s new wave"], 8, "local:"), (["80s new wave"], 100, "ytm:"), ([], 8, "local:")],
)
async def test_persona_lane_local_fallback_and_empty_taste(library, taste, minimum, prefix):
    dj, _, _ = blend_dj(library, local_min_matches=minimum, local_min_artists=3)
    dj.taste_read = lambda: taste
    selection = await dj._lane_batch("persona", "ytm")
    assert selection.tracks[0].uri.startswith(prefix)
    if prefix == "ytm:":
        assert "80s new wave" in selection.playlist


async def test_same_source_boundary_enqueues_while_last_song_plays(library):
    dj, _, local = blend_dj(
        library, genre_batch=2, queue_top_up=1, blend={"library": 1, "persona": 0, "discovery": 0}
    )
    await dj.seed("Song 1", source="library")
    dj._radio_count = 2
    dj.reserve.clear()
    local.queued.clear()
    playing = local.current
    plays = list(local.calls)
    await dj.top_up()
    assert local.current == playing and local.calls == plays
    assert len(local.queued) == 2 and dj._radio_count == 2


def test_play_stats_both_sources_last_90_days(library, capsys):
    from datetime import timedelta

    from familiar_music.commands import _run_status

    now = datetime.now(UTC)
    library.record_play(library.resolve("local:0")[1], now.isoformat(), "played", 180)
    library.record_play(Track("ytm:id", "Video", ("Other",)), now.isoformat(), "skipped", 5)
    library.record_play(
        Track("ytm:old", "Old", ("Old",)), (now - timedelta(days=91)).isoformat(), "played", 180
    )
    _run_status(library)
    output = capsys.readouterr().out
    assert "Plays recorded: 3" in output
    assert "by played (last 90 days): Band 0 (1)" in output
    assert "by skipped (last 90 days): Other (1)" in output
    assert "Old" not in output


@pytest.mark.parametrize(
    "key,value",
    [
        ("skip_early_s", -1),
        ("played_fraction", 0),
        ("played_fraction", 1.1),
        ("taste_half_life_days", 0),
        ("played_gain", -1),
        ("skip_penalty", -1),
        ("taste_min", 0),
        ("taste_max", 0.1),
        ("artist_cooldown_songs", 1.5),
        ("artist_cooldown_songs", True),
        ("artist_batch_share", 0),
        ("artist_batch_share", 1.1),
        ("artist_cooldown_weight", 1.1),
        ("occasional_genres", "salsa"),
        ("occasional_genres", [""]),
        ("occasional_weight", 0),
        ("occasional_weight", float("nan")),
        ("min_track_s", -1),
        ("local_min_artists", 0),
        ("local_min_artists", -1),
        ("local_min_artists", 1.5),
        ("local_min_artists", True),
        ("local_min_artists", float("inf")),
        ("artist_tag_min_share", 0),
        ("artist_tag_min_share", -0.1),
        ("artist_tag_min_share", 1.1),
        ("artist_tag_min_share", True),
        ("artist_tag_min_share", float("nan")),
    ],
)
def test_invalid_taste_settings(key, value):
    from familiar.interfaces import ConfigError
    from familiar_music.settings import music_settings

    with pytest.raises(ConfigError):
        music_settings({key: value})


async def test_track_weight_and_relevance_from_distinct_terms(library):
    from familiar_music.library.query import expand
    from familiar_music.sources import artist_key

    now = datetime.now(UTC)
    track = library.resolve("local:0")[1]
    for _ in range(5):
        library.record_play(track, now.isoformat(), "played", 180)
    dj, _, _ = blend_dj(library)
    source = dj.sources["local"]
    facts = library.selection_facts(60, now)
    _, years, terms = expand("80s new wave")
    weighted = source._weight(track, facts, terms, years, None, artist_key(track), False)
    assert weighted == pytest.approx(2 * 1.75**1.5)
    other = library.resolve("local:3")[1]
    assert source._weight(other, facts, terms, years, None, artist_key(other), False) == (
        pytest.approx(2 * 1.75**0.5)
    )
    assert source._behaviour("never", {}) == 1
    assert source._behaviour(track.uri, {(track.uri, "skipped"): 1000}) == 0.2
    assert source._behaviour(track.uri, {(track.uri, "played"): 1000}) == 3


async def test_familiar_more_like_this_change_is_ignored(library, monkeypatch):
    player, now = FakePlayer(), [0]
    service = PearMusicService(
        {}, player=player, searcher=FakeSearch(), index=library, clock=lambda: now[0]
    )
    await service.poll()

    async def changing_pick():
        player.current = Track("ytm:other", "Other")
        return {"status": "picked"}

    monkeypatch.setattr(service.dj, "reseed", changing_pick)
    now[0] = 10
    await service._execute("more_like_this")
    await service.aclose()
    assert library.play_stats()["plays_recorded"] == 0


async def test_behaviour_observation_failure_does_not_fail_skip(library, monkeypatch):
    player = FakePlayer()
    service = PearMusicService({}, player=player, searcher=FakeSearch(), index=library)

    async def broken_observation():
        raise OSError("player metadata temporarily unavailable")

    monkeypatch.setattr(player, "now_playing", broken_observation)
    assert await service._execute("skip_track") == {"status": "Skipped track"}
    assert player.calls == ["skip"]
    await service.aclose()


async def test_persona_match_threshold_counts_candidates_before_batch_constraints(library):
    with library._connect() as conn:
        conn.execute("UPDATE tracks SET artist = 'Solo', artist_norm = 'solo'")
    dj, _, _ = blend_dj(library, local_min_artists=1)
    dj.taste_read = lambda: ["new wave"]
    selection = await dj._lane_batch("persona", "ytm")
    assert selection.tracks[0].uri.startswith("local:")
    assert selection.matches == 18
    assert selection.match_artists == 1
    assert len(selection.tracks) == 1


@pytest.mark.parametrize("artists", [3, 4])
@pytest.mark.parametrize("lane", ["request", "persona", "radio"])
async def test_local_genre_requires_artist_variety(library, artists, lane):
    with library._connect() as conn:
        conn.execute(
            "UPDATE tracks SET artist = 'Band ' || (CAST(uuid AS INTEGER) % ?), "
            "artist_norm = 'band ' || (CAST(uuid AS INTEGER) % ?)",
            (artists, artists),
        )
    dj, _, _ = blend_dj(library, genre_batch=2, playlist_tracks=2)
    # Both counts describe all eligible tracks, even when the queue is capped at two.
    eligible = await dj.sources["local"].genre("new wave")
    assert eligible.matches == 18 and eligible.match_artists == artists
    if lane == "request":
        result = await dj.seed("new wave", kind="genre")
        assert result["source"] == {3: "YouTube Music", 4: "library"}[artists]
        return
    if lane == "persona":
        dj.taste_read = lambda: ["new wave"]
        selection = await dj._lane_batch("persona", "ytm")
        assert selection.tracks[0].uri.startswith({3: "ytm:", 4: "local:"}[artists])
        return
    dj._radio_track = Track("ytm:seed", "Seed", genre="New Wave")
    selection = await dj._radio_batch("local", "ytm")
    assert (selection is not None) == (artists == 4)


async def test_forced_library_genre_ignores_both_minimums(library):
    dj, _, _ = blend_dj(library, local_min_matches=100, local_min_artists=100)
    result = await dj.seed("new wave", kind="genre", source="library")
    assert result["source"] == "library"


async def test_genre_artist_count_uses_primary_artists_and_eligible_tracks(library):
    with library._connect() as conn:
        conn.execute(
            "UPDATE tracks SET artist = 'Band 0 feat. Guest', artist_norm = 'band 0' "
            "WHERE artist_norm = 'band 1'"
        )
        conn.execute("UPDATE tracks SET duration_s = 20 WHERE artist_norm = 'band 2'")
    dj, _, _ = blend_dj(library)
    dj.recent.append("local:0")
    dj.dislikes.add(library.resolve("local:1")[1])
    selection = await dj.sources["local"].genre("new wave")
    assert selection.match_artists == 1
    assert selection.matches == 10


async def test_local_genre_passes_artist_tag_share_override(library):
    from familiar_music.library.index import store_artist_tags

    with library._connect() as conn:
        store_artist_tags(conn, "band 0", {"rock": 100, "jazz": 25})
    dj, _, _ = blend_dj(library, artist_tag_min_share=0.5)
    assert await dj.sources["local"].genre("jazz") is None
