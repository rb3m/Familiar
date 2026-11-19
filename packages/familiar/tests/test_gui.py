"""Offline coverage for the launcher contract and subprocess lifecycle."""

import asyncio
import json
import signal
import sys
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from familiar import session_settings
from familiar.config import AppConfig, BackendConfig, load_config
from familiar.gui import server
from familiar.gui.personas import save_persona
from familiar.gui.session import SessionConflictError, SessionManager
from familiar.interfaces import ConfigError
from familiar.persona import list_personas, load_persona, validate_persona
from familiar.session_settings import SessionSettings


def persona(persona_id="custom"):
    return dict(
        id=persona_id,
        display_name="Companion",
        source="Original",
        prompt="Be kind.",
        chattiness=0.5,
        style_rules=[],
        game_hooks={},
        truthfulness="strict",
        languages={"en": {"voices": {"gemini": "Kore", "elevenlabs": None}}},
    )


class DirectClient:
    """Exercise router and middleware when the sandbox disables TCP sockets."""

    def __init__(self, app):
        self.server = SimpleNamespace(app=app)
        self.session = SimpleNamespace(headers={"X-Familiar-Token": "secret"})
        app.freeze()

    def make_url(self, path):
        return f"http://127.0.0.1:8765{path}"

    async def request(self, method, path, *, headers=None, json=None, data=None):
        request = make_mocked_request(
            method,
            path,
            app=self.server.app,
            headers={**self.session.headers, **(headers or {})},
            transport=SimpleNamespace(get_extra_info=lambda *_: ("127.0.0.1", 8765)),
        )

        async def read_json():
            if data is not None:
                raise ValueError("Invalid JSON")
            return json

        request.json = read_json
        response = await self.server.app._handle(request)

        async def response_json():
            import json as json_module

            return json_module.loads(response.body)

        return SimpleNamespace(status=response.status, json=response_json)

    async def get(self, path, **kwargs):
        return await self.request("GET", path, **kwargs)

    async def put(self, path, **kwargs):
        return await self.request("PUT", path, **kwargs)

    async def post(self, path, **kwargs):
        return await self.request("POST", path, **kwargs)

    async def delete(self, path, **kwargs):
        return await self.request("DELETE", path, **kwargs)


@pytest.fixture(params=["direct", "http"])
async def client(tmp_path, request):
    config = AppConfig(
        personas_dir=tmp_path / "builtin",
        user_personas_dir=tmp_path / "user",
        ptt=BackendConfig("evdev"),
    )
    save_persona(config.personas_dir, persona("mira"))
    app = server.create_app(config, token="secret", session_path=tmp_path / "session.toml")
    if request.param == "direct":
        yield DirectClient(app)
        return
    # Preserve real TestServer coverage on hosts that permit loopback sockets.
    import socket

    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except PermissionError:
        pytest.skip("Sandbox denies loopback TCP sockets")
    probe.close()
    async with TestClient(TestServer(app, shutdown_timeout=0.01)) as client:
        client.session.headers["X-Familiar-Token"] = "secret"
        yield client


@pytest.mark.asyncio
async def test_auth_and_state(client):
    for headers in (
        {"X-Familiar-Token": ""},
        {"X-Familiar-Token": "bad"},
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
    ):
        response = await client.get("/api/state", headers=headers)
        assert response.status == 403
        assert "error" in await response.json()
    response = await client.get(
        "/api/state", headers={"Origin": str(client.make_url("/")).rstrip("/")}
    )
    assert response.status == 200
    state = await response.json()
    assert set(state) == {"games", "personas", "languages", "defaults", "session", "quota"}
    assert state["quota"] is None
    assert state["personas"][0]["builtin"] is True
    assert "roles" in state["personas"][0]
    assert "aliases" in state["personas"][0]
    assert set(state["session"]) == {
        "running",
        "game",
        "persona",
        "language",
        "crew",
        "carrier",
        "banter",
        "started_at",
        "exit_code",
    }


@pytest.mark.asyncio
async def test_persona_crud(client):
    assert (await client.delete("/api/personas/mira")).status == 400
    for data in (
        {},
        {**persona(), "chattiness": True},
        {**persona(), "languages": []},
        {**persona(), "truthfulness": []},
    ):
        assert (await client.put("/api/personas/mira", json=data)).status == 400
    for bad_id in ("UPPER", "bad.id", "-bad", "a" * 41):
        assert (await client.put(f"/api/personas/{bad_id}", json=persona())).status == 400
    assert (await (await client.get("/api/state")).json())["personas"][0][
        "overrides_builtin"
    ] is False
    response = await client.put("/api/personas/mira", json=persona("ignored"))
    saved = await response.json()
    assert saved["builtin"] is False
    assert saved["overrides_builtin"] is True
    data = await (await client.get("/api/personas/mira")).json()
    assert data["id"] == "mira"
    assert data["builtin"] is False
    assert data["overrides_builtin"] is True
    state = await (await client.get("/api/state")).json()
    assert len(state["personas"]) == 1
    assert state["personas"][0]["builtin"] is False
    assert state["personas"][0]["overrides_builtin"] is True
    custom = await (await client.put("/api/personas/custom", json=persona())).json()
    assert custom["overrides_builtin"] is False
    assert (await client.delete("/api/personas/mira")).status == 200
    restored = await (await client.get("/api/personas/mira")).json()
    assert restored["builtin"] is True
    assert restored["overrides_builtin"] is False
    assert (await client.delete("/api/personas/missing")).status == 404
    assert (await client.put("/api/personas/custom", data="garbage")).status == 400
    assert (await client.delete("/api/personas/custom")).status == 200


async def test_persona_music_taste_round_trips(client):
    tastes = {"music": ["70s city pop", "quiet folk"], "food": ["ramen"]}
    saved = await client.put("/api/personas/custom", json={**persona(), "tastes": tastes})
    assert saved.status == 200
    data = await (await client.get("/api/personas/custom")).json()
    assert data["tastes"] == tastes
    bad = {**persona(), "tastes": {"music": "city pop"}}
    assert (await client.put("/api/personas/custom", json=bad)).status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("fenced", [False, True, None])
async def test_draft(client, monkeypatch, fenced):
    data = persona()
    data.pop("languages")
    reply = json.dumps(data)
    if fenced:
        reply = f"```json\n{reply}\n```"
    if fenced is None:
        reply = "garbage"
    closed = []

    class LLM:
        async def stream(self, messages, system, tools):
            assert "Complete example" in system
            assert "strict" in system and "playful" in system
            assert "properties" in system
            yield SimpleNamespace(text=reply)

        async def aclose(self):
            closed.append(True)

    def load(kind, name, settings):
        assert kind == "llm" and settings["temperature"] == 0.7
        return LLM()

    monkeypatch.setattr(server, "load", load)
    response = await client.post("/api/personas/draft", json={"character": "Mira"})
    assert response.status == (502 if fenced is None else 200)
    result = await response.json()
    if fenced is not None:
        assert result["languages"]["en"]["voices"] == {"gemini": "Kore", "elevenlabs": None}
        assert (await client.get("/api/personas/custom")).status == 404
    assert closed


@pytest.mark.asyncio
async def test_voices_and_quota(client, monkeypatch):
    calls = []

    class TTS:
        async def voices(self):
            return [{"voice_id": "v", "name": "Voice", "labels": {"accent": "British"}}]

        async def quota(self):
            calls.append("quota")
            return 120, 10000

        async def aclose(self):
            pass

    def load(kind, name, settings):
        assert settings["api_key_env"] == "ELEVENLABS_API_KEY"
        return TTS()

    monkeypatch.setattr(server, "load", load)
    data = await (await client.get("/api/voices")).json()
    assert len(data["gemini"]) == 30
    assert data["elevenlabs"][0]["id"] == "v"
    assert data["elevenlabs_error"] is None
    api = client.server.app[server.API]
    api.config.tts = BackendConfig(
        "gemini", fallback=BackendConfig("elevenlabs", {"api_key_env": "ELEVENLABS_API_KEY"})
    )
    for _ in range(2):
        state = await (await client.get("/api/state")).json()
        assert state["quota"] == {"used": 120, "limit": 10000}
    assert calls == ["quota"]

    def fail(*args):
        raise RuntimeError("offline")

    monkeypatch.setattr(server, "load", fail)
    data = await (await client.get("/api/voices")).json()
    assert data["elevenlabs"] == [] and data["elevenlabs_error"] == "offline"


@pytest.mark.asyncio
async def test_voice_sample(client, monkeypatch):
    called = []

    class TTS:
        def synthesize(self, text, voice, language):
            called.append((text, voice, language))
            return "audio"

        async def aclose(self):
            called.append("tts closed")

    class Player:
        async def play(self, audio):
            assert audio == "audio"

        async def wait_idle(self):
            pass

        async def aclose(self):
            called.append("player closed")

    monkeypatch.setattr(server, "load", lambda *args: TTS())
    monkeypatch.setattr(server, "Player", Player)
    response = await client.post(
        "/api/voices/test", json=dict(backend="gemini", voice="Kore", language="en")
    )
    assert response.status == 200
    assert called[0][1:] == ("Kore", "en")
    assert len(called) == 3


@pytest.mark.asyncio
async def test_session_broadcast_replay_conflict_stop(tmp_path, monkeypatch):
    manager = SessionManager(AppConfig(ptt=BackendConfig("evdev")), tmp_path / "config.toml")
    command = manager.command("mira", "en", "ats")
    assert command[:5] == [sys.executable, "-u", "-m", "familiar", "voice"]
    assert command[-2:] == ["--config", str(tmp_path / "config.toml")]
    script = (
        "import time,sys; print('hello',flush=True); "
        "print('error',file=sys.stderr,flush=True); time.sleep(30)"
    )
    monkeypatch.setattr(manager, "command", lambda *args: [sys.executable, "-u", "-c", script])
    queue = manager.subscribe()
    assert (await manager.start("mira", "en"))["running"]
    with pytest.raises(SessionConflictError):
        await manager.start("mira", "en")
    events = [await asyncio.wait_for(queue.get(), 3) for _ in range(3)]
    assert events[0][0] == "session"
    assert {item[1]["text"] for item in events[1:]} == {"hello", "error"}
    assert len(manager.lines) == 2
    state = await manager.stop()
    assert not state["running"] and state["exit_code"] is not None
    assert await manager.stop() == state


@pytest.mark.asyncio
async def test_ring_and_exit(monkeypatch):
    manager = SessionManager(AppConfig(ptt=BackendConfig("evdev")))
    script = "import sys; [print(i) for i in range(250)]; sys.exit(7)"
    monkeypatch.setattr(manager, "command", lambda *args: [sys.executable, "-c", script])
    await manager.start("mira", "en")
    await asyncio.wait_for(manager._monitor, 3)
    assert len(manager.lines) == 200
    assert manager.lines[0]["text"] == "50"
    assert manager.state["exit_code"] == 7


@pytest.mark.asyncio
async def test_terminal_refusal():
    manager = SessionManager(AppConfig())
    with pytest.raises(ConfigError, match="familiar ptt-setup"):
        await manager.start("mira", "en")
    assert manager.process is None


@pytest.mark.asyncio
async def test_kill_fallback(monkeypatch):
    manager = SessionManager(AppConfig(ptt=BackendConfig("evdev")))
    actions = []

    class Process:
        returncode = None

        def send_signal(self, sig):
            assert sig == signal.SIGINT
            actions.append("interrupt")

        def terminate(self):
            actions.append("terminate")

        def kill(self):
            actions.append("kill")
            self.returncode = -9

        async def wait(self):
            return self.returncode

    async def wait_for(awaitable, timeout):
        await awaitable
        if timeout is not None:
            raise TimeoutError

    manager.process = Process()
    monkeypatch.setattr(asyncio, "wait_for", wait_for)
    await manager.stop()
    assert actions == ["interrupt", "terminate", "kill"]


@pytest.mark.asyncio
async def test_sse_replay_and_events(client):
    if isinstance(client, DirectClient):
        pytest.skip("Streaming transport is covered by the HTTP fixture")
    manager = client.server.app[server.API].sessions
    manager.lines.append(dict(stream="stdout", text="replayed", ts="2026-11-15T00:00:00+00:00"))
    response = await client.get("/api/events?token=secret")

    async def event():
        kind = (await response.content.readline()).decode().strip()
        data = (await response.content.readline()).decode().strip()
        assert await response.content.readline() == b"\n"
        return kind, json.loads(data.removeprefix("data: "))

    assert (await event())[0] == "event: line"
    assert (await event())[0] == "event: session"
    manager.broadcast("line", dict(stream="stderr", text="live", ts="now"))
    assert (await event())[1]["text"] == "live"
    manager.broadcast("session", manager.state)
    assert (await event())[0] == "event: session"
    response.close()


def test_config_and_store(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text('user_personas_dir = "user"\n')
    config = load_config(config_file)
    assert config.user_personas_dir == tmp_path / "user"
    save_persona(tmp_path / "builtin", persona())
    save_persona(config.user_personas_dir, {**persona(), "display_name": "User"})
    loaded = load_persona(tmp_path / "builtin", "custom", config.user_personas_dir)
    assert loaded.display_name == "User"
    assert list_personas([config.user_personas_dir, tmp_path / "builtin"]) == [(loaded, False)]
    assert not list(config.user_personas_dir.glob("*.tmp"))
    for bad in ("../bad", "", "Bad", "a" * 41):
        with pytest.raises(ConfigError):
            validate_persona(persona(bad))
    assert asdict(loaded)["id"] == "custom"


@pytest.mark.asyncio
async def test_sse_without_socket(tmp_path, monkeypatch):
    api = server.APIService(AppConfig(user_personas_dir=tmp_path), None, "secret")
    api.sessions.lines.append(dict(stream="stdout", text="replay", ts="now"))
    emitted = []

    class Response:
        def __init__(self, headers):
            assert headers["Content-Type"] == "text/event-stream"

        async def prepare(self, request):
            pass

        async def write(self, raw):
            emitted.append(raw.decode())
            if len(emitted) == 2:
                api.sessions.broadcast("line", dict(stream="stderr", text="live", ts="now"))
            if len(emitted) == 3:
                api.shutdown.set()

    monkeypatch.setattr(server.web, "StreamResponse", Response)
    await api.events(SimpleNamespace())
    assert [item.splitlines()[0] for item in emitted] == [
        "event: line",
        "event: session",
        "event: line",
    ]
    assert "replay" in emitted[0] and "live" in emitted[2]
    assert api.clients == 0 and not api.sessions.subscribers


@pytest.mark.asyncio
async def test_real_kill_and_graceful_exit(monkeypatch):
    original_wait_for = asyncio.wait_for
    for ignore in (False, True):
        manager = SessionManager(AppConfig(ptt=BackendConfig("evdev")))
        handler = "signal.SIG_IGN" if ignore else "lambda *_: sys.exit(0)"
        script = (
            "import signal,sys,time; "
            f"signal.signal(signal.SIGINT, {handler}); "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready',flush=True); time.sleep(30)"
        )
        monkeypatch.setattr(
            manager, "command", lambda *args, script=script: [sys.executable, "-c", script]
        )
        queue = manager.subscribe()
        await manager.start("mira", "en")
        await queue.get()
        assert (await original_wait_for(queue.get(), 3))[1]["text"] == "ready"

        async def fast_wait(awaitable, timeout):
            return await original_wait_for(awaitable, 0.05 if timeout is not None else None)

        monkeypatch.setattr(asyncio, "wait_for", fast_wait)
        state = await manager.stop()
        assert state["exit_code"] == (-signal.SIGKILL if ignore else 0)
        assert not state["running"]


@pytest.mark.asyncio
async def test_session_endpoints_and_shutdown(client, monkeypatch):
    api = client.server.app[server.API]
    command = "import time; print('ready',flush=True); time.sleep(30)"
    monkeypatch.setattr(api.sessions, "command", lambda *args: [sys.executable, "-c", command])
    response = await client.post(
        "/api/session/start", json=dict(persona="mira", language="en", game=None)
    )
    assert response.status == 200
    assert (await response.json())["running"]
    assert (await client.post("/api/session/start", json={})).status == 409
    assert (await client.post("/api/session/stop")).status == 200
    assert not api.sessions.state["running"]
    assert (await client.post("/api/shutdown")).status == 200
    await asyncio.wait_for(api.shutdown.wait(), 1)


@pytest.mark.asyncio
async def test_session_settings_defaults_without_file(client, tmp_path):
    path = tmp_path / "session.toml"
    assert not path.exists()
    response = await client.get("/api/session/settings")
    assert response.status == 200
    assert await response.json() == {
        "version": 2,
        "game": None,
        "crew": ["mira"],
        "language": "en",
        "carrier": "mira",
        "banter": 2,
        "stereo": False,
        "seats": {"mira": 0.0},
        "problems": [],
    }
    assert not path.exists()


@pytest.mark.asyncio
async def test_session_settings_invalid_file_reported_without_writing(client, tmp_path):
    path = tmp_path / "session.toml"
    path.write_text(
        'version = 1\ncrew = ["mira", "mira"]\ncarrier = "ghost"\nlanguage = "en"\nbanter = 99\n'
    )
    before = path.read_bytes()
    response = await client.get("/api/session/settings")
    assert response.status == 200
    data = await response.json()
    assert data["crew"] == ["mira", "mira"]
    assert data["carrier"] == "ghost"
    assert data["banter"] == 99
    problems = data["problems"]
    assert any("unique" in problem for problem in problems)
    assert any("carrier" in problem for problem in problems)
    assert any("banter" in problem for problem in problems)
    assert path.read_bytes() == before


@pytest.mark.asyncio
async def test_put_session_settings(client, tmp_path):
    api = client.server.app[server.API]
    path = tmp_path / "session.toml"
    good = {
        "version": 1,
        "game": None,
        "crew": ["mira"],
        "language": "en",
        "carrier": "mira",
        "banter": 5,
    }
    response = await client.put("/api/session/settings", json=good)
    assert response.status == 200
    assert (await response.json())["banter"] == 5
    loaded = session_settings.load(
        path,
        personas_dir=api.config.personas_dir,
        user_personas_dir=api.config.user_personas_dir,
    )
    assert loaded.crew == ("mira",) and loaded.banter == 5
    bad = {
        **good,
        "game": "",
        "crew": ["mira", "mira"],
        "language": "",
        "carrier": "ghost",
        "banter": -1,
    }
    response = await client.put("/api/session/settings", json=bad)
    assert response.status == 400
    data = await response.json()
    assert data["error"]
    problems = data["problems"]
    assert any("game" in problem for problem in problems)
    assert any("unique" in problem for problem in problems)
    assert any("language" in problem for problem in problems)
    assert any("carrier" in problem for problem in problems)
    assert any("banter" in problem for problem in problems)
    assert (
        session_settings.load(
            path,
            personas_dir=api.config.personas_dir,
            user_personas_dir=api.config.user_personas_dir,
        ).banter
        == 5
    )


@pytest.mark.asyncio
async def test_put_session_settings_stereo_seats_round_trip(client, tmp_path):
    api = client.server.app[server.API]
    path = tmp_path / "session.toml"
    save_persona(api.config.personas_dir, persona("frieren"))
    good = {
        "version": 2,
        "game": None,
        "crew": ["mira", "frieren"],
        "language": "en",
        "carrier": "mira",
        "banter": 5,
        "stereo": True,
        "seats": {"mira": 0.0, "frieren": -0.7},
    }
    response = await client.put("/api/session/settings", json=good)
    assert response.status == 200
    saved = await response.json()
    assert saved["stereo"] is True
    assert saved["seats"] == {"mira": 0.0, "frieren": -0.7}
    loaded = session_settings.load(
        path,
        personas_dir=api.config.personas_dir,
        user_personas_dir=api.config.user_personas_dir,
    )
    assert loaded.stereo is True
    assert loaded.seats == {"mira": 0.0, "frieren": -0.7}
    response = await client.get("/api/session/settings")
    assert response.status == 200
    echoed = await response.json()
    assert echoed["version"] == 2
    assert echoed["stereo"] is True
    assert echoed["seats"] == {"mira": 0.0, "frieren": -0.7}
    assert echoed["problems"] == []
    for seats in ({"ghost": 0.5}, {"mira": 1.5}, {"mira": True}):
        response = await client.put("/api/session/settings", json={**good, "seats": seats})
        assert response.status == 400
        assert any("seats" in problem for problem in (await response.json())["problems"])


@pytest.mark.asyncio
async def test_carrier_must_be_in_crew(client, tmp_path):
    body = {
        "version": 1,
        "game": None,
        "crew": ["mira"],
        "language": "en",
        "carrier": "ghost",
        "banter": 2,
    }
    response = await client.put("/api/session/settings", json=body)
    assert response.status == 400
    assert any("carrier" in problem for problem in (await response.json())["problems"])
    assert not (tmp_path / "session.toml").exists()


@pytest.mark.asyncio
async def test_start_uses_session_file(client, tmp_path, monkeypatch):
    api = client.server.app[server.API]
    settings = SessionSettings(crew=("mira",), language="en", carrier="mira", banter=4)
    session_settings.save(tmp_path / "session.toml", settings)
    command = api.sessions.session_command()
    assert command[:5] == [sys.executable, "-u", "-m", "familiar", "voice"]
    assert ["--session", str(tmp_path / "session.toml")] == command[5:7]
    assert "--persona" not in command
    assert "--language" not in command
    assert "--game" not in command
    script = "import time; print('ready',flush=True); time.sleep(30)"
    monkeypatch.setattr(
        api.sessions, "session_command", lambda: [sys.executable, "-u", "-c", script]
    )
    response = await client.post("/api/session/start")
    assert response.status == 200
    state = await response.json()
    assert state["running"]
    assert state["crew"] == ["mira"]
    assert state["carrier"] == "mira"
    assert state["banter"] == 4
    assert state["persona"] == "mira"
    assert (await client.post("/api/session/stop")).status == 200


@pytest.mark.asyncio
async def test_start_with_invalid_session_file(client, tmp_path):
    (tmp_path / "session.toml").write_text(
        'version = 1\ncrew = ["mira"]\ncarrier = "ghost"\nlanguage = "en"\nbanter = 3\n'
    )
    response = await client.post("/api/session/start", json={})
    assert response.status == 400
    data = await response.json()
    assert any("carrier" in problem for problem in data["problems"])
    assert not client.server.app[server.API].sessions.state["running"]


@pytest.mark.asyncio
async def test_legacy_start_body_still_works(client, monkeypatch):
    api = client.server.app[server.API]
    command = "import time; print('ready',flush=True); time.sleep(30)"
    monkeypatch.setattr(api.sessions, "command", lambda *args: [sys.executable, "-c", command])
    response = await client.post(
        "/api/session/start", json=dict(persona="mira", language="en", game=None)
    )
    assert response.status == 200
    state = await response.json()
    assert state["running"]
    assert state["crew"] == ["mira"]
    assert state["carrier"] == "mira"
    assert state["banter"] is None
    assert not (api.session_path).exists()
    assert (await client.post("/api/session/stop")).status == 200


def test_sanitize_draft_voices_keeps_real_gemini_and_clears_elevenlabs():
    from familiar.gui.server import sanitize_draft_voices

    drafted = {
        "id": "x",
        "languages": {
            "en": {"voices": {"gemini": "Puck", "elevenlabs": "InventedId123"}},
            "ja": {"voices": {"gemini": "NotAVoice"}},
        },
    }
    cleaned = sanitize_draft_voices(drafted)["languages"]
    assert cleaned["en"]["voices"] == {"gemini": "Puck", "elevenlabs": None}
    assert cleaned["ja"]["voices"] == {"gemini": "Kore", "elevenlabs": None}
    assert sanitize_draft_voices({"id": "x"})["languages"] == {
        "en": {"voices": {"gemini": "Kore", "elevenlabs": None}}
    }


@pytest.mark.asyncio
async def test_packaged_persona_override_and_reset(tmp_path):
    api = server.APIService(AppConfig(user_personas_dir=tmp_path), None, "test")
    original, builtin = api.persona("mira")
    assert builtin
    assert not api.overrides_builtin("mira")
    save_persona(tmp_path, {**original, "display_name": "Custom Mira"})
    assert api.persona("mira")[0]["display_name"] == "Custom Mira"
    assert api.overrides_builtin("mira")
    request = SimpleNamespace(match_info={"id": "mira"})
    await api.delete_persona(request)
    assert api.persona("mira") == (original, True)
    assert not api.overrides_builtin("mira")
