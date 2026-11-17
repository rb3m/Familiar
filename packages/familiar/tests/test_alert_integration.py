"""Shared runtime registration and an offline player-to-alert round trip."""

import argparse
import asyncio
import json
from contextlib import AsyncExitStack
from types import SimpleNamespace

import pytest
from familiar import cli
from familiar.config import AppConfig
from familiar.interfaces import LLMChunk, ToolCall, WatchSample, WatchSpec
from familiar.voice import VoiceSession


class Adapter:
    game = "test"

    def __init__(self):
        self.sample = WatchSample({"altitude": 1000}, "v1", True, True, 0)

    def describe(self):
        return "Altitude: 1000 metres"

    def watchables(self):
        return {"altitude": WatchSpec("m", "altitude above sea level")}

    def watch_sample(self):
        return self.sample

    async def snapshot(self):
        return {"connected": True, "paused": False}

    async def events(self):
        await asyncio.Event().wait()
        yield  # pragma: no cover


class LLM:
    async def stream(self, messages, system, tools):
        if messages[-1].role == "tool":
            result = json.loads(messages[-1].text)
            assert (result["value"], result["unit"], result["si_value"]) == (4, "km", 4000)
            yield LLMChunk(text="I'll tell you at four kilometres.")
            return
        if not tools:
            yield LLMChunk(text="Passing four thousand metres now.")
            return
        assert "Never claim an alert is set unless set_alert returned it" in system
        yield LLMChunk(
            tool_call=ToolCall(
                "a",
                "set_alert",
                {
                    "quantity": "altitude",
                    "direction": "above",
                    "value": 4,
                    "unit": "km",
                    "announcement": "Four kilometres!",
                },
            )
        )


@pytest.mark.parametrize("enabled,watchable", [(True, True), (False, True), (True, False)])
async def test_shared_builder_registers_only_enabled_capability(
    tmp_path, monkeypatch, persona, enabled, watchable
):
    adapter = Adapter() if watchable else SimpleNamespace(game="test", describe=lambda: "")
    monkeypatch.setattr(cli, "load", lambda kind, *_: adapter if kind == "games" else LLM())
    config = AppConfig(
        game="test",
        transcript_dir=tmp_path,
        memory={"enabled": False},
        flavor={"enabled": False},
        alerts={"enabled": enabled},
    )
    async with AsyncExitStack() as stack:
        conversation, _, _ = cli._Runtime(config, persona, "en", stack).conversation(
            argparse.Namespace(game=None)
        )
        expected = {"set_alert", "list_alerts", "cancel_alert"} if enabled and watchable else set()
        assert {spec.name for spec in conversation.tools.specs()} == expected
        assert (conversation.alerts is not None) == bool(expected)


async def build(tmp_path, monkeypatch, persona, stack):
    adapter = Adapter()
    monkeypatch.setattr(cli, "load", lambda kind, *_: adapter if kind == "games" else LLM())
    config = AppConfig(
        game="test", transcript_dir=tmp_path, memory={"enabled": False}, flavor={"enabled": False}
    )
    return cli._Runtime(config, persona, "en", stack).conversation(argparse.Namespace(game=None))


async def test_player_request_confirmation_poll_and_interjection(tmp_path, monkeypatch, persona):
    async with AsyncExitStack() as stack:
        conversation, adapter, director = await build(tmp_path, monkeypatch, persona, stack)
        assert [s async for s in conversation.ask("Tell me at four kilometres")] == [
            "I'll tell you at four kilometres."
        ]
        director.set_game_state(True, False)
        adapter.sample = WatchSample({"altitude": 4100}, "v1", True, True, 0.5)
        task = asyncio.create_task(conversation.alerts.run(director.submit))
        try:
            event = await asyncio.wait_for(director.next(), 1)
            assert event.kind == "alert"
            assert [s async for s in conversation.interject(event)] == [
                "Four kilometres!"  # spoken verbatim, no LLM call
            ]
            assert not conversation.alerts.alerts
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_voice_session_starts_and_cancels_alert_watcher(tmp_path, monkeypatch, persona):
    started, stopped = asyncio.Event(), asyncio.Event()

    async def in_thread(function, *args):
        return function(*args)

    monkeypatch.setattr("familiar.voice.asyncio.to_thread", in_thread)
    async with AsyncExitStack() as stack:
        conversation, adapter, director = await build(tmp_path, monkeypatch, persona, stack)

        async def watcher(submit):
            assert submit == director.submit
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        conversation.alerts = SimpleNamespace(run=watcher)

        async def presses():
            await asyncio.wait_for(started.wait(), 1)
            return
            yield  # pragma: no cover

        session = VoiceSession(
            SimpleNamespace(events=presses),
            SimpleNamespace(stop=lambda: None),
            SimpleNamespace(stop=lambda: None),
            object(),
            object(),
            conversation,
            persona,
            "en",
            conversation.transcript,
            adapter,
            director,
        )
        await asyncio.wait_for(session.run(), 2)
        assert stopped.is_set()
