"""Additive service wiring and voice failure isolation."""

import argparse
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from familiar import cli
from familiar.config import AppConfig, load_config
from familiar.director import Director
from familiar.interfaces import GameEvent, ServiceTool, SpeechState, ToolSpec
from m1_support import run
from test_m1_voice import make_session
from test_m2_director import persona


class Service:
    name = "test"

    def __init__(self):
        self.states = []
        self.closed = False
        self.fail = False

    async def speech_state(self, state):
        self.states.append(state)
        if self.fail:
            raise RuntimeError("broken service")

    async def events(self):
        yield GameEvent("music_track", "ambient", {"summary": "A song"}, datetime.now(UTC))
        raise RuntimeError("broken feed")

    async def aclose(self):
        self.closed = True

    def describe(self):
        return "Now playing: Music"

    def service_tools(self):
        async def handler(**arguments):
            return {"status": "done"}

        return [
            ServiceTool(ToolSpec("play_music", "Play", {}), handler, True),
            ServiceTool(ToolSpec("now_playing", "Read", {}), handler, False),
        ]


@pytest.mark.parametrize("with_game", [False, True])
async def test_runtime_loads_service_and_registers_dj_tools(monkeypatch, tmp_path, with_game):
    from test_m2_voice import FakeGameAdapter

    service = Service()
    adapter = FakeGameAdapter({})
    loaded = []

    def load(kind, name, settings):
        loaded.append((kind, settings))
        return {"services": service, "games": adapter, "llm": object()}[kind]

    monkeypatch.setattr(cli, "load", load)
    config = AppConfig(
        music={"enabled": True, "service": "test", "duck_level": 0.3},
        memory={"path": tmp_path / "memory.db"},
        transcript_dir=tmp_path,
    )
    async with AsyncExitStack() as stack:
        runtime = cli._Runtime(config, persona(), "en", stack)
        conversation, _, director = runtime.conversation(
            argparse.Namespace(game="ats" if with_game else None)
        )
        assert isinstance(director, Director)
        assert [s.name for s in conversation.tools.specs(())] == ["now_playing"]
        assert {s.name for s in conversation.tools.specs()} == {"play_music", "now_playing"}
        assert "Now playing: Music" in conversation.context()
        settings = next(settings for kind, settings in loaded if kind == "services")
        assert settings["taste_read"]() == conversation.crew.dj.tastes.get("music", [])
        conversation.crew.dj = replace(conversation.crew.dj, tastes={"music": ["jazz"]})
        assert settings["taste_read"]() == ["jazz"]
        conversation.crew.dj = replace(conversation.crew.dj, tastes={})
        assert settings["taste_read"]() == []
        settings["dislike_write"]("track", ("Artist",))
        assert settings["dislike_read"]()[0] == {"track"}
    assert service.closed


@pytest.mark.parametrize("failure", [False, True])
def test_voice_state_and_failure_isolation(failure):
    async def scenario():
        service = Service()
        service.fail = failure
        session = make_session(services=[service])
        await session.run()
        assert session.player.audio == ["First sentence.", "Second sentence."]
        assert any(state.listening for state in service.states)
        assert any(state.speaking for state in service.states)
        assert service.states[-1] == SpeechState()
        assert service.closed

    run(scenario())


async def test_service_events_bypass_disconnected_game_and_fail_safely():
    service = Service()
    director = Director(0.5, {})
    director.set_game_state(False, True)
    session = make_session(services=[service], director=director)
    await session._service_events(service)
    director.set_game_state(False, True)
    event = director._take()
    assert event.kind == "music_track"
    session._event_kind = event.kind
    session._busy("speaking", True)
    session._busy("critical", True)
    await session._flush_speech()
    assert service.states[-1] == SpeechState(False, True, True, "music_track")
    session._busy("speaking", False)
    session._busy("critical", False)
    await session._flush_speech()
    assert service.states[-1] == SpeechState()


def test_music_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FAMILIAR_CONFIG", raising=False)
    path = tmp_path / "empty.toml"
    path.write_text("")
    assert load_config(path).music == {"enabled": False, "service": "pear"}


def test_slow_service_never_holds_speech(monkeypatch):
    import asyncio
    import time

    from familiar import voice

    monkeypatch.setattr(voice, "SERVICE_FLUSH_S", 0.01)

    class SlowService(Service):
        async def speech_state(self, state):
            self.states.append(state)
            await asyncio.sleep(5)

    async def scenario():
        service = SlowService()
        session = make_session(services=[service])
        started = time.monotonic()
        await session.run()
        assert session.player.audio == ["First sentence.", "Second sentence."]
        assert time.monotonic() - started < 2

    run(scenario())


@pytest.mark.parametrize("lines", [[], ["Rain at last."]])
def test_unprompted_turn_ducks_only_once_it_has_a_line(lines):
    """A dropped scene or empty remark must not dip the music for nothing."""

    async def sentences():
        for line in lines:
            yield line

    async def scenario():
        from time import monotonic

        service = Service()
        session = make_session(services=[service])
        event = GameEvent("banter", "ambient", {"summary": "scene"}, datetime.now(UTC))
        timings = dict.fromkeys(("stt_ms", "first_sentence_ms", "first_audio_ms"))
        await session._speak(sentences(), monotonic(), timings, event)
        await session._flush_speech()
        assert any(state.speaking for state in service.states) == bool(lines)
        assert service.states == [] or service.states[-1] == SpeechState()
        assert "pending" not in session._speech_busy

    run(scenario())
