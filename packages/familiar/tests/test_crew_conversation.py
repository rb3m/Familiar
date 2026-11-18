"""Per-speaker prompts, shared protocol history, permissions and audible delivery."""

import json
from contextlib import AsyncExitStack, aclosing
from dataclasses import replace

import pytest
from familiar.cli import _Runtime
from familiar.config import AppConfig, BackendConfig
from familiar.conversation import Conversation
from familiar.crew import Crew
from familiar.hands import ActionKeeper
from familiar.history import DefaultHistoryRenderer
from familiar.interfaces import ActionSpec, AudioChunk, BackendError, LLMChunk, Message, ToolCall
from familiar.tools import ToolRegistry
from familiar.transcript import TranscriptLog
from familiar.voice import VoiceSession
from test_crew import event
from test_tools import LLM, SPEC, assert_complete


def conversation(tmp_path, crew, rounds, **kwargs):
    return Conversation(
        LLM(rounds), crew.carrier, "en", TranscriptLog(tmp_path), crew=crew, **kwargs
    )


async def consume(source):
    return [line async for line in source]


async def test_roster_roles_relationships_and_single_speaker_history(tmp_path, crew):
    frieren = replace(crew.members[1], relationships={"mira": "My trusted ship AI."})
    crew = Crew([crew.carrier, frieren], "mira")
    chat = conversation(
        tmp_path,
        crew,
        [
            [LLMChunk(text="Mira: Hello.")],
            [LLMChunk(text="Frieren: Indeed.")],
            [LLMChunk(text="Yes.")],
        ],
        game="ats",
    )
    assert await consume(chat.ask("Mira, hello")) == ["Hello."]
    assert await consume(chat.ask("Frieren, what do you think?")) == ["Indeed."]
    messages, system, _ = chat.llm.requests[1]
    assert "You are Frieren; speak only as Frieren" in system
    assert "My trusted ship AI." in system
    assert "Notice the road." in system
    assert "Mira: Hello." in system
    assert not any(message.role == "assistant" for message in messages)
    assert await consume(chat.ask("Go on")) == ["Yes."]
    assert Message("assistant", "Indeed.") in chat.llm.requests[2][0]
    assert [record.speaker_id for record in chat.records] == [
        "player",
        "mira",
        "player",
        "frieren",
        "player",
        "frieren",
    ]
    records = [json.loads(line) for line in chat.transcript.path.read_text().splitlines()]
    assert [record["speaker_id"] for record in records] == [
        "player",
        "mira",
        "player",
        "frieren",
        "player",
        "frieren",
    ]
    assert records[1]["text"] == "Hello."
    assert records[1]["heard"] is True


async def test_single_crew_exact_prompt_and_messages(tmp_path, persona):
    chat = Conversation(
        LLM([[LLMChunk(text="Hello.")], [LLMChunk(text="Again.")]]),
        persona,
        "en",
        TranscriptLog(tmp_path),
    )
    await consume(chat.ask("Hi"))
    await consume(chat.ask("Continue"))
    assert chat.llm.requests[0][:2] == ([Message("user", "Hi")], persona.system_prompt("en"))
    assert chat.llm.requests[1][:2] == (
        [Message("user", "Hi"), Message("assistant", "Hello."), Message("user", "Continue")],
        persona.system_prompt("en"),
    )


async def test_renderer_is_swappable(tmp_path, crew):
    class Renderer(DefaultHistoryRenderer):
        def render(self, *args):
            system, messages = super().render(*args)
            return system + "\nCustom renderer.", messages

    chat = conversation(tmp_path, crew, [[LLMChunk(text="Hi.")]], renderer=Renderer())
    await consume(chat.ask("Hi"))
    assert "Custom renderer." in chat.llm.requests[0][1]


async def test_tool_owner_keeps_payload_others_see_outcome(tmp_path, crew):
    async def lookup(value):
        return {"status": "done", "detail": "Found a system", "private_payload": "sensitive"}

    tools = ToolRegistry()
    tools.register(SPEC, lookup)
    call = ToolCall("a", "lookup", {"value": 1}, {"signature": "opaque"})
    chat = conversation(
        tmp_path,
        crew,
        [
            [LLMChunk(tool_call=call)],
            [LLMChunk(text="Found it.")],
            [LLMChunk(text="I see.")],
            [LLMChunk(text="Again.")],
        ],
        tools=tools,
    )
    await consume(chat.ask("Mira, look"))
    await consume(chat.ask("Frieren?"))
    messages, system, _ = chat.llm.requests[2]
    assert "Found a system" in system
    assert "private_payload" not in system and "sensitive" not in system
    assert not any(message.tool_calls or message.role == "tool" for message in messages)
    await consume(chat.ask("Mira?"))
    messages, _, _ = chat.llm.requests[3]
    assert_complete(messages)
    assert any(message.tool_calls == (call,) for message in messages)
    assert any("private_payload" in message.text for message in messages)


class Actuator:
    def __init__(self):
        self.performed = []

    def actions(self):
        return {
            "query": ActionSpec("Look", {}, read_only=True),
            "move": ActionSpec("Move", {}),
            "stage": ActionSpec("Stage", {}, risky=True),
        }

    async def perform(self, name, arguments):
        self.performed.append(name)
        return {"status": "done", "detail": name}


async def test_noncarrier_has_core_queries_and_refuses_mutation_even_if_hallucinated(
    tmp_path, crew
):
    tools = ToolRegistry()
    actuator = Actuator()
    actions = ActionKeeper(actuator, {}, "en")
    actions.register(tools)

    async def lookup(value):
        return {"answer": value}

    tools.register(SPEC, lookup)
    calls = [
        ToolCall("a", "act_move", {}),
        ToolCall("b", "act_query", {}),
        ToolCall("c", "confirm_action", {"confirmation_id": "invented"}),
    ]
    chat = conversation(
        tmp_path,
        crew,
        [[LLMChunk(tool_call=call) for call in calls], [LLMChunk(text="Ask Mira to move.")]],
        tools=tools,
        actions=actions,
    )
    await consume(chat.ask("Frieren, move us"))
    offered = {spec.name for spec in chat.llm.requests[0][2]}
    assert offered == {"lookup", "act_query"}
    assert "name Mira" in chat.llm.requests[0][1]
    assert actuator.performed == ["query"]
    assert actions.pending is None
    assert_complete(chat.history)


async def test_carrier_confirmation_survives_noncarrier_turn_and_interjections_no_tools(
    tmp_path, crew
):
    tools = ToolRegistry()
    actuator = Actuator()
    actions = ActionKeeper(actuator, {}, "en")
    actions.register(tools)
    chat = conversation(
        tmp_path,
        crew,
        [
            [LLMChunk(tool_call=ToolCall("a", "act_stage", {}))],
            [LLMChunk(text="Confirm?")],
            [LLMChunk(text="Ask Mira.")],
            [LLMChunk(text="Event.")],
        ],
        tools=tools,
        actions=actions,
    )
    await consume(chat.ask("Mira, stage"))
    pending = actions.pending
    await consume(chat.ask("Frieren, yes"))
    assert actions.pending is pending
    assert actuator.performed == []
    await consume(chat.interject(event()))
    assert chat.speaker.id == "frieren"
    assert chat.llm.requests[-1][2] == []
    chat.llm.rounds = iter(
        [
            [LLMChunk(tool_call=ToolCall("b", "confirm_action", {"confirmation_id": pending.id}))],
            [LLMChunk(text="Done.")],
        ]
    )
    await consume(chat.ask("Mira, yes"))
    assert actuator.performed == ["stage"]


async def test_interrupted_text_is_attributed_cut_off_and_not_spoken(tmp_path, crew):
    chat = conversation(
        tmp_path, crew, [[LLMChunk(text="Frieren: First. Second.")], [LLMChunk(text="Hello.")]]
    )
    async with aclosing(chat.ask("Frieren?")) as source:
        assert await anext(source) == "First."
    assert chat.records[-1].heard is False
    assert chat.crew.last_speaker is None
    await consume(chat.ask("Mira?"))
    assert "Frieren: [Cut off; not heard in full] First. Second." in chat.llm.requests[-1][1]


async def test_prefix_split_across_chunks_before_storage_and_tts(tmp_path, crew):
    chat = conversation(tmp_path, crew, [[LLMChunk(text="Frie"), LLMChunk(text="ren: Hello.")]])
    assert await consume(chat.ask("Frieren?")) == ["Hello."]
    assert chat.records[-1].text == "Hello."


@pytest.mark.parametrize("audible", [True, False])
async def test_voice_uses_speaker_voice_and_only_acknowledges_drained_audio(
    tmp_path, crew, audible
):
    chat = conversation(tmp_path, crew, [[LLMChunk(text="Frieren: Hello.")]])
    heard = []
    voices = []
    frieren = replace(crew.members[1], languages={"en": {"voices": {"fake": "frieren-voice"}}})
    chat.crew = Crew([crew.carrier, frieren], "mira")

    class TTS:
        name = "fake"

        async def synthesize(self, text, voice, language):
            voices.append(voice)
            if not audible:
                return
            yield AudioChunk(b"00", 16000)

    class Player:
        async def play(self, chunks, pan=0.0):
            async for _ in chunks:
                pass

        async def wait_idle(self):
            if heard:
                return
            assert not chat.records or chat.records[-1].kind != "assistant"
            heard.append(True)

    session = VoiceSession(
        None, None, Player(), None, TTS(), chat, crew.carrier, "en", chat.transcript
    )
    timings = dict.fromkeys(("first_sentence_ms", "first_audio_ms"))
    await session._speak(chat.ask("Frieren?"), 0, timings)
    assert voices == ["frieren-voice"]
    assert heard
    assert chat.records[-1].heard is audible
    assert (chat.crew.last_speaker == frieren) is audible


async def test_fallback_voices_follow_current_speaker(tmp_path, crew, monkeypatch):
    from familiar import cli

    voices = []

    class TTS:
        def __init__(self, name):
            self.name = name

        async def synthesize(self, text, voice, language):
            voices.append((self.name, voice))
            if self.name == "primary":
                raise BackendError("down")
            yield AudioChunk(b"00", 16000)

    members = [
        replace(
            member,
            languages={
                "en": {
                    "voices": {
                        "primary": member.id + "-primary",
                        "secondary": member.id + "-secondary",
                    }
                }
            },
        )
        for member in crew.members
    ]
    crew = Crew(members, "mira")
    monkeypatch.setattr(cli, "load", lambda kind, name, settings: TTS(name))
    async with AsyncExitStack() as stack:
        runtime = _Runtime(AppConfig(), crew.carrier, "en", stack, crew)
        tts = runtime.backend("tts", BackendConfig("primary", fallback=BackendConfig("secondary")))
        chat = conversation(tmp_path, crew, [])
        runtime.active_conversation = chat
        chat.speaker = crew.members[1]
        assert [chunk async for chunk in tts.synthesize("Hi", None, "en")]
        chat.speaker = crew.carrier
        assert [chunk async for chunk in tts.synthesize("Hi", None, "en")]
    assert voices == [
        ("primary", "frieren-primary"),
        ("secondary", "frieren-secondary"),
        ("secondary", "mira-secondary"),
    ]


async def test_preformatted_critical_alert_uses_carrier_without_llm(tmp_path, crew):
    from dataclasses import replace

    chat = conversation(tmp_path, crew, [])
    chat.crew.note_spoke(crew.members[1])
    alert = replace(
        event("alert", "critical"), data={"summary": "Low fuel", "speak": "Mira: Low fuel."}
    )
    assert await consume(chat.interject(alert)) == ["Low fuel."]
    assert chat.speaker == crew.carrier
    assert not chat.llm.requests
    assert chat.records[-1].speaker_id == "mira" and chat.records[-1].heard
    assert crew.last_speaker == crew.carrier


async def test_unplayed_preformatted_alert_is_cut_off(tmp_path, crew):
    from dataclasses import replace

    chat = conversation(tmp_path, crew, [])
    alert = replace(event("alert", "critical"), data={"summary": "Low fuel", "speak": "Low fuel."})
    async with aclosing(chat.interject(alert)) as source:
        assert await anext(source) == "Low fuel."
    assert not chat.records[-1].heard
    assert crew.last_speaker is None


async def test_own_unheard_line_is_context_not_a_spoken_model_turn(tmp_path, crew):
    chat = conversation(
        tmp_path, crew, [[LLMChunk(text="First. Never heard.")], [LLMChunk(text="Next.")]]
    )
    async with aclosing(chat.ask("Frieren?")) as source:
        assert await anext(source) == "First."
    await consume(chat.ask("Frieren, continue"))
    messages, system, _ = chat.llm.requests[-1]
    assert "Cut-off generations (not said in full)" in system
    assert "Never heard." in system
    assert not any(message.role == "assistant" for message in messages)


async def test_transcript_keeps_order_and_roster_uses_third_person_roles(tmp_path, crew):
    mira = replace(crew.carrier, roles={"ats": "the dispatch AI"})
    crew = Crew([mira, crew.members[1]], "mira")
    chat = conversation(
        tmp_path,
        crew,
        [[LLMChunk(text="Hello.")], [LLMChunk(text="Indeed.")]],
        game="ats",
    )
    await consume(chat.ask("Mira, hello"))
    await consume(chat.ask("Frieren, do you agree?"))
    system = chat.llm.requests[1][1]
    assert system.index("Player: Mira, hello") < system.index("Mira: Hello.")
    assert system.index("Mira: Hello.") < system.index("Player: Frieren, do you agree?")
    assert "- Mira (mira): the dispatch AI" in system
    assert "- Frieren (frieren): crew member" in system
