"""Shared game setup, lazy command dispatch, and text-mode background events."""

import argparse
import threading
from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace

from familiar import cli
from familiar.config import AppConfig, BackendConfig
from familiar.conversation import Conversation
from familiar.director import Director
from familiar.interfaces import LLMChunk
from m1_support import run
from test_m2_director import Log, event, persona
from test_m2_voice import FakeGameAdapter


def test_runtime_game_override_and_context(monkeypatch, tmp_path) -> None:
    loaded = []
    adapter = FakeGameAdapter({})
    llm = object()

    def load(kind, name, settings):
        loaded.append((kind, name, settings))
        return {"games": adapter, "llm": llm}[kind]

    monkeypatch.setattr(cli, "load", load)

    async def scenario() -> None:
        config = AppConfig(
            game="other",
            games={"ats": {"units": "imperial"}},
            transcript_dir=tmp_path,
            llm=BackendConfig("fake"),
        )
        async with AsyncExitStack() as stack:
            runtime = cli._Runtime(config, persona(), "en", stack)
            conversation, game, director = runtime.conversation(argparse.Namespace(game="ats"))
            assert game is adapter and isinstance(director, Director)
            assert conversation.game == "ats"
            assert conversation.context() == "Fuel: 20 L"
            assert loaded[0] == ("games", "ats", {"units": "imperial"})

    run(scenario())


def test_text_game_events_while_waiting_for_input(monkeypatch, capsys) -> None:
    seen = threading.Event()
    calls = 0
    log = Log()
    character = persona()
    adapter = FakeGameAdapter({})
    director = Director(0.5, {})

    class LLM:
        async def stream(self, messages, system, tools):
            yield LLMChunk("Hello.")
            seen.set()

    conversation = Conversation(LLM(), character, "en", log)

    @asynccontextmanager
    async def runtime(args):
        yield SimpleNamespace(
            persona=character, conversation=lambda args: (conversation, adapter, director)
        )

    def read(prompt):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert seen.wait(2), "A game event must run while input waits in its thread"
            return "question"
        return "/quit"

    monkeypatch.setattr(cli, "_runtime", runtime)
    monkeypatch.setattr("builtins.input", read)

    async def scenario() -> None:
        await adapter.queue.put(event("landmark"))
        await cli._chat(argparse.Namespace())

    run(scenario())
    assert "Test (event: landmark): Hello." in capsys.readouterr().out
    assert [kind for kind, _ in log.records] == ["event", "assistant", "user", "assistant"]
