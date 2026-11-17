"""Core action consent never depends on the model policing itself."""

import argparse
import asyncio
from contextlib import AsyncExitStack
from copy import deepcopy
from types import SimpleNamespace

import pytest
from familiar.cli import _Runtime
from familiar.config import HANDS_DEFAULTS, AppConfig, load_config
from familiar.conversation import ACTION_RULE, Conversation
from familiar.hands import ActionKeeper
from familiar.interfaces import ActionSpec, BackendError, ConfigError, GameEvent, LLMChunk, ToolCall
from familiar.tools import ToolRegistry

SCHEMA = {"type": "object", "properties": {"value": {"type": "integer"}}}


class Actuator:
    game = "fake"
    context = "vessel-a"
    keyboard = None

    def __init__(self):
        self.calls = []

    def actions(self):
        return {
            "light": ActionSpec("Turn on light", SCHEMA),
            "stage": ActionSpec("Stage vessel", SCHEMA, risky=True),
            "abort": ActionSpec("Abort vessel", SCHEMA, risky=True),
            "throttle": ActionSpec(
                "Set throttle", SCHEMA, risky_when=lambda args: args["value"] > 0
            ),
        }

    async def perform(self, name, arguments):
        self.calls.append((name, deepcopy(arguments)))
        return {"status": "done", "detail": name}

    def action_context(self):
        return self.context

    def attach_keyboard(self, keyboard):
        self.keyboard = keyboard

    def describe(self):
        return ""


@pytest.fixture
def gate():
    adapter = Actuator()
    keeper = ActionKeeper(adapter, {}, "en")
    registry = ToolRegistry()
    keeper.register(registry)
    keeper.begin_turn("please act")
    return adapter, keeper, registry


async def call(registry, name, **arguments):
    return await registry.execute(ToolCall("call", name, arguments))


@pytest.mark.asyncio
async def test_harmless_dedupe_and_new_turn(gate):
    adapter, keeper, registry = gate
    result = await call(registry, "act_light", value=1)
    assert result == await call(registry, "act_light", value=1)
    assert len(adapter.calls) == 1
    keeper.begin_turn("again")
    await call(registry, "act_light", value=1)
    assert len(adapter.calls) == 2
    assert any(row["decision"] == "deduplicated" for row in keeper.history)


@pytest.mark.asyncio
async def test_risky_later_turn_frozen_arguments_and_single_execution(gate):
    adapter, keeper, registry = gate
    args = {"value": 1}
    pending = await call(registry, "act_stage", **args)
    assert pending["status"] == "needs_confirmation"
    assert adapter.calls == []
    args["value"] = 9
    confirmation_id = pending["confirmation_id"]
    keeper.utterance = "yes"  # Model self-confirming in a later tool round still fails.
    assert (await call(registry, "confirm_action", confirmation_id=confirmation_id))[
        "status"
    ] == "error"
    keeper.begin_turn("yes")
    assert (await call(registry, "confirm_action", confirmation_id=confirmation_id))[
        "status"
    ] == "done"
    assert (await call(registry, "confirm_action", confirmation_id=confirmation_id))[
        "status"
    ] == "error"
    assert adapter.calls == [("stage", {"value": 1})]
    # A repeated act call after confirming in this turn also cannot execute again.
    await call(registry, "act_stage", value=1)
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", ["yes, abort", "no", "don't", "wait", "sí, cancela", "はい、待って"]
)
async def test_negative_cancels(gate, text):
    adapter, keeper, registry = gate
    pending = await call(registry, "act_stage")
    keeper.begin_turn(text)
    assert keeper.pending is None
    assert (await keeper.confirm_action(pending["confirmation_id"]))["status"] == "error"
    assert not adapter.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,text",
    [("abort", "yes, abort"), ("stage", "sí"), ("stage", "了解です"), ("stage", "okay, nominal")],
)
async def test_multilingual_and_action_name_exception(gate, name, text):
    adapter, keeper, registry = gate
    pending = await call(registry, "act_" + name)
    keeper.begin_turn(text)
    assert (await keeper.confirm_action(pending["confirmation_id"]))["status"] == "done"
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_clarifying_turn_and_clean_yes(gate):
    adapter, keeper, registry = gate
    pending = await call(registry, "act_stage")
    keeper.begin_turn("which stage?")
    assert (await keeper.confirm_action(pending["confirmation_id"]))["status"] == "error"
    assert keeper.pending is not None
    keeper.begin_turn("yes")
    await keeper.confirm_action(pending["confirmation_id"])
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["ttl", "context", "id"])
async def test_confirmation_guards(gate, monkeypatch, failure):
    adapter, keeper, registry = gate
    monkeypatch.setattr("familiar.hands.time.monotonic", lambda: 100)
    pending = await call(registry, "act_stage")
    keeper.begin_turn("yes")
    if failure == "ttl":
        monkeypatch.setattr("familiar.hands.time.monotonic", lambda: 131)
    if failure == "context":
        adapter.context = "vessel-b"
    confirmation_id = "wrong" if failure == "id" else pending["confirmation_id"]
    assert (await keeper.confirm_action(confirmation_id))["status"] == "error"
    assert not adapter.calls


@pytest.mark.asyncio
async def test_replacement_cancel_and_risk_predicate(gate):
    adapter, keeper, registry = gate
    first = await call(registry, "act_stage")
    second = await call(registry, "act_abort")
    assert first["confirmation_id"] != second["confirmation_id"]
    assert keeper.pending.name == "abort"
    await call(registry, "cancel_action")
    assert keeper.pending is None
    assert (await call(registry, "act_throttle", value=0))["status"] == "done"
    assert (await call(registry, "act_throttle", value=1))["status"] == "needs_confirmation"
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_timeout_no_retry_and_exception(gate, monkeypatch):
    adapter, keeper, registry = gate
    calls = []

    async def slow(name, arguments):
        calls.append(name)
        await asyncio.sleep(1)

    monkeypatch.setattr(adapter, "perform", slow)
    keeper.action_timeout_s = 0.001
    result = await call(registry, "act_light")
    assert result == {"status": "unknown", "detail": "timed out; outcome unknown, not retried"}
    assert await call(registry, "act_light") == result
    assert calls == ["light"]

    async def broken(name, arguments):
        raise RuntimeError("disconnected")

    monkeypatch.setattr(adapter, "perform", broken)
    keeper.begin_turn("light please")
    assert (await call(registry, "act_light")) == {"status": "error", "detail": "disconnected"}


@pytest.mark.asyncio
async def test_extra_words(gate):
    adapter, _, _ = gate
    keeper = ActionKeeper(adapter, {"affirmatives": {"es": ["venga"]}}, "en")
    keeper.begin_turn("stage")
    result = await keeper.request("stage", adapter.actions()["stage"])
    keeper.begin_turn("venga")
    assert (await keeper.confirm_action(result["confirmation_id"]))["status"] == "done"


@pytest.mark.parametrize(
    "body",
    [
        "hands = false",
        "[hands]\nenabled = 1",
        '[hands]\nkeyboard = ""',
        "[hands]\nkeyboard = false",
        "[hands]\nconfirm_ttl_s = 0",
        "[hands]\naction_timeout_s = -1",
        "[hands]\naction_timeout_s = true",
        "[hands]\nconfirm_ttl_s = inf",
        "[hands]\naffirmatives = []",
        "[hands.negatives]\nen = [1]",
        '[hands.affirmatives]\nes = "sí"',
    ],
)
def test_hands_invalid_config(tmp_path, body):
    path = tmp_path / "config.toml"
    path.write_text(body)
    with pytest.raises(ConfigError):
        load_config(path)


def test_hands_config_defaults_and_words(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("")
    assert load_config(path).hands == HANDS_DEFAULTS
    path.write_text('[hands]\nconfirm_ttl_s = 40\n[hands.affirmatives]\nes = ["venga"]')
    settings = load_config(path).hands
    assert settings["confirm_ttl_s"] == 40
    assert settings["affirmatives"] == {"es": ["venga"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("keyboard_failure,enabled", [(False, True), (True, True), (False, False)])
async def test_cli_wiring(tmp_path, monkeypatch, caplog, keyboard_failure, enabled):
    adapter = Actuator()
    keyboard = SimpleNamespace(closed=False)

    async def close():
        keyboard.closed = True

    keyboard.aclose = close
    loads = []

    def load(kind, name, settings):
        loads.append((kind, name))
        if kind == "games":
            return adapter
        if kind == "keyboard":
            if keyboard_failure:
                raise BackendError("no uinput")
            return keyboard
        return SimpleNamespace()

    monkeypatch.setattr("familiar.cli.load", load)
    config = AppConfig(
        transcript_dir=tmp_path,
        memory={"enabled": False},
        flavor={"enabled": False},
        hands={"enabled": enabled},
    )
    persona = SimpleNamespace(chattiness=0.5)
    async with AsyncExitStack() as stack:
        runtime = _Runtime(config, persona, "en", stack)
        conversation, _, _ = runtime.conversation(argparse.Namespace(game="fake"))
        assert (conversation.actions is not None) == enabled
        if enabled:
            assert "act_stage" in [spec.name for spec in conversation.tools.specs()]
            assert ("keyboard", "uinput") in loads
        assert (adapter.keyboard is keyboard) == (enabled and not keyboard_failure)
    assert keyboard.closed == (enabled and not keyboard_failure)
    if keyboard_failure:
        assert "no uinput" in caplog.text


@pytest.mark.asyncio
async def test_conversation_player_only_and_self_confirmation(gate):
    adapter, keeper, registry = gate
    prompts = []

    class LLM:
        async def stream(self, messages, system, tools):
            prompts.append((system, tools))
            if len(prompts) == 1:
                yield LLMChunk(tool_call=ToolCall("request", "act_stage", {}))
                return
            if len(prompts) == 2:
                yield LLMChunk(
                    tool_call=ToolCall(
                        "confirm", "confirm_action", {"confirmation_id": keeper.pending.id}
                    )
                )
                return
            yield LLMChunk("Ready.")

    transcript = SimpleNamespace(record=lambda *args, **kwargs: None)
    persona = SimpleNamespace(system_prompt=lambda *args: "persona")
    conversation = Conversation(LLM(), persona, "en", transcript, tools=registry, actions=keeper)
    assert [s async for s in conversation.ask("stage please")] == ["Ready."]
    assert keeper.turn == 2
    assert not adapter.calls
    assert ACTION_RULE in prompts[0][0]
    event = GameEvent("test", "ambient", {"summary": "event"}, None)
    assert [s async for s in conversation.interject(event)] == ["Ready."]
    assert keeper.turn == 2
    assert prompts[-1][1] == []
    assert ACTION_RULE not in prompts[-1][0]


@pytest.mark.asyncio
async def test_stored_nested_arguments_and_optional_context():
    calls = []

    async def perform(name, arguments):
        calls.append(arguments)
        # Extra result fields must not collide with history metadata.
        return {"status": "done", "detail": name, "name": name, "decision": "pack detail"}

    adapter = SimpleNamespace(perform=perform)
    keeper = ActionKeeper(adapter, {}, "en")
    keeper.begin_turn("please stage")
    arguments = {"nested": {"steps": [1, 2]}}
    spec = ActionSpec("Stage", {}, risky=True)
    pending = await keeper.request("stage", spec, **arguments)
    arguments["nested"]["steps"].append(3)
    keeper.begin_turn("yes")
    result = await keeper.confirm_action(pending["confirmation_id"])
    assert result["status"] == "done"
    assert calls == [{"nested": {"steps": [1, 2]}}]
    assert await keeper.request("stage", spec, nested={"steps": [1, 2]}) == result


@pytest.mark.asyncio
async def test_cancelled_execution_is_not_retried(gate, monkeypatch):
    adapter, keeper, registry = gate
    started = asyncio.Event()
    calls = []

    async def perform(name, arguments):
        calls.append(name)
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(adapter, "perform", perform)
    task = asyncio.create_task(call(registry, "act_light"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await call(registry, "act_light"))["status"] == "unknown"
    assert calls == ["light"]
    assert any(row["decision"] == "interrupted" for row in keeper.history)
