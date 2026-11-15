"""Offline arbitration and telemetry conversation contracts."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from familiar.config import load_config
from familiar.conversation import Conversation
from familiar.director import Director
from familiar.interfaces import ConfigError, GameEvent, LLMChunk
from familiar.persona import Persona


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Log:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def record(self, kind: str, **fields) -> None:
        self.records.append((kind, fields))


def event(kind: str = "fuel", priority: str = "notable", **data) -> GameEvent:
    return GameEvent(kind, priority, {"summary": kind, **data}, datetime.now(UTC))


def director(chattiness: float = 0.5, **settings) -> tuple[Director, Clock]:
    clock = Clock()
    result = Director(chattiness, {"idle_minutes": 100, **settings}, clock)
    result.set_game_state(True, False)
    return result, clock


async def held(d: Director) -> None:
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(d.next(), 0.015)


def test_priority_bound_and_dedupe() -> None:
    async def scenario() -> None:
        d, _ = director()
        for index in range(10):
            d.submit(event(str(index), "ambient"))
        d.submit(event("warning", "critical"))
        assert (await d.next()).kind == "warning"
        assert len(d._queue) == 7
        d, _ = director()
        d.submit(event("fuel", "ambient"))
        replacement = event("fuel", "notable")
        d.submit(replacement)
        d.submit(event("damage", "critical"))
        assert (await d.next()).kind == "damage"
        assert await d.next() == replacement
        await held(d)
        d.submit(event("landmark_nearby", id="a"))
        d.submit(event("landmark_nearby", id="b"))
        d.submit(event("landmark_nearby", id="a", summary="new"))
        assert len(d._queue) == 2
        assert (await d.next()).data["id"] == "b"

    asyncio.run(scenario())


def test_ttl_and_log_only() -> None:
    async def scenario() -> None:
        d, clock = director()
        for priority, ttl in [("critical", 60), ("notable", 90), ("ambient", 30)]:
            d.submit(event(priority, priority))
            assert d._queue[-1].deadline <= clock.now + ttl
        clock.now = 91
        await held(d)
        d.submit(event("custom", ttl_s=2))
        clock.now += 3
        await held(d)
        d.submit(event("log", "critical", ttl_s=0))
        d.submit(
            GameEvent(
                "old", "critical", {"summary": "old"}, datetime.now(UTC) - timedelta(seconds=61)
            )
        )
        await held(d)

    asyncio.run(scenario())


@pytest.mark.parametrize("chattiness,scale", [(0, 1.5), (0.5, 1), (1, 0.5)])
@pytest.mark.parametrize("priority,cooldown", [("notable", 40), ("ambient", 300)])
def test_scaled_cooldowns(chattiness: float, scale: float, priority: str, cooldown: int) -> None:
    async def scenario() -> None:
        d, clock = director(chattiness)
        d.notify_spoke()
        d.submit(event("first", priority, ttl_s=1000))
        clock.now = cooldown * scale - 0.1
        await held(d)
        clock.now += 0.1
        assert (await d.next()).kind == "first"
        d.notify_spoke()
        d.submit(event("second", priority, ttl_s=1000))
        await held(d)

    asyncio.run(scenario())


def test_kind_cooldown_critical_and_busy() -> None:
    async def scenario() -> None:
        d, clock = director()
        d.submit(event())
        await d.next()
        d.submit(event(ttl_s=1000))
        clock.now = 119
        await held(d)
        clock.now = 120
        await d.next()
        d.notify_spoke()
        d.set_busy("speaking", True)
        d.submit(event("critical", "critical"))
        assert d.preempts(await d.next())
        d.submit(event("critical", "critical"))
        d.set_busy("user", True)
        await held(d)
        d.set_busy("user", False)
        assert (await d.next()).kind == "critical"
        d.submit(event("other", ttl_s=1000))
        clock.now += 50
        await held(d)
        d.set_busy("speaking", False)
        assert (await d.next()).kind == "other"
        d.set_busy("other-reason", True)
        d.submit(event("critical", "critical"))
        await held(d)

    asyncio.run(scenario())


@pytest.mark.parametrize("connected,paused", [(False, False), (True, True)])
def test_paused_disconnected_hold_and_drop(connected: bool, paused: bool) -> None:
    async def scenario() -> None:
        d, clock = director()
        d.submit(event("ambient", "ambient"))
        d.set_game_state(connected, paused)
        d.submit(event("notable"))
        d.submit(event("critical", "critical", ttl_s=1))
        d.submit(event("ambient2", "ambient"))
        await held(d)
        clock.now = 2
        d.set_game_state(True, False)
        assert (await d.next()).kind == "notable"
        await held(d)

    asyncio.run(scenario())


def test_idle_reset_and_disabled() -> None:
    async def scenario() -> None:
        d, clock = director(1, idle_minutes=12)
        clock.now = 359
        await held(d)
        clock.now = 360
        assert (await d.next()).kind == "idle"
        await held(d)
        d.notify_spoke()
        clock.now = 719
        await held(d)
        clock.now = 720
        assert (await d.next()).kind == "idle"
        d, clock = director(enabled=False, idle_minutes=0)
        d.submit(event("critical", "critical"))
        clock.now = 1000
        await held(d)

    asyncio.run(scenario())


def persona() -> Persona:
    return Persona(
        "test", "Test", "tests", 0.5, "Be brief.", [], {"en": {}}, {"ats": "You are in a truck."}
    )


def test_context_and_interjection_history_transcript() -> None:
    class LLM:
        def __init__(self) -> None:
            self.calls: list[tuple[list, str]] = []

        async def stream(self, messages, system, tools):
            self.calls.append((messages, system))
            yield LLMChunk("Hello. ")
            yield LLMChunk("Safe travels.")

    async def scenario() -> None:
        llm, log = LLM(), Log()
        situation = ["Fuel: 10 L"]
        conversation = Conversation(
            llm, persona(), "en", log, context=lambda: situation[0], game="ats"
        )
        assert [s async for s in conversation.ask("Fuel?")] == ["Hello.", "Safe travels."]
        situation[0] = "Fuel: 9 L"
        assert [s async for s in conversation.interject(event(facts=["Exact fact"]))]
        assert "You are in a truck." in llm.calls[0][1]
        assert llm.calls[0][1].endswith(
            "Current situation (exact, from live telemetry):\nFuel: 10 L"
        )
        assert llm.calls[-1][1].endswith("Fuel: 9 L")
        assert "[Game event, not said by the player." in llm.calls[-1][0][-1].text
        assert "Facts you may use: - Exact fact" in llm.calls[-1][0][-1].text
        assert conversation.history[-1].role == "assistant"
        records = [fields for kind, fields in log.records if kind == "event"]
        assert records[-1] == {"event_kind": "fuel", "priority": "notable", "summary": "fuel"}
        assert len([kind for kind, _ in log.records if kind == "user"]) == 1
        situation[0] = ""
        assert [s async for s in conversation.ask("Hi")]
        assert "Current situation" not in llm.calls[-1][1]

    asyncio.run(scenario())


def test_game_config(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'game = "ats"\n[games.ats]\nunits = "imperial"\n'
        "[director]\nenabled = false\nidle_minutes = 4\n"
        "notable_cooldown_s = 7\nambient_cooldown_s = 9\n"
    )
    config = load_config(path)
    assert config.game == "ats"
    assert config.games == {"ats": {"units": "imperial"}}
    assert config.director == {
        "enabled": False,
        "idle_minutes": 4,
        "notable_cooldown_s": 7,
        "ambient_cooldown_s": 9,
    }
    path.write_text("")
    config = load_config(path)
    assert config.game is None and config.games == {} and config.director == {}


@pytest.mark.parametrize("text", ["game = 3", "games = []", "director = 4", "[games]\nats = 1"])
def test_invalid_game_config(tmp_path, text: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(text)
    with pytest.raises(ConfigError):
        load_config(path)


def test_interjection_cancellation_keeps_partial_reply() -> None:
    class LLM:
        async def stream(self, messages, system, tools):
            yield LLMChunk("First sentence. ")
            await asyncio.Event().wait()

    async def scenario() -> None:
        log = Log()
        conversation = Conversation(LLM(), persona(), "en", log)
        started = asyncio.Event()

        async def consume() -> None:
            async for _sentence in conversation.interject(event()):
                started.set()

        task = asyncio.create_task(consume())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert conversation.history[-1].text == "First sentence. "
        assert log.records[-1] == ("assistant", {"text": "First sentence. ", "interrupted": True})

    asyncio.run(scenario())
