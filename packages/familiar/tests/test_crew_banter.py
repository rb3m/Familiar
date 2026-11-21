"""Offline banter budget, scene validation and shared playback arbitration."""

import asyncio
import json
import random
from contextlib import aclosing
from dataclasses import replace
from types import SimpleNamespace

import pytest
from familiar.banter import Budget, Casting, limits, parse_scene
from familiar.conversation import Conversation
from familiar.crew import Crew
from familiar.director import Director
from familiar.history import DefaultHistoryRenderer
from familiar.interfaces import AudioChunk, LLMChunk, ToolCall
from familiar.transcript import TranscriptLog
from familiar.voice import VoiceSession
from test_m2_director import Clock, event
from test_tools import LLM


@pytest.mark.parametrize(
    "level,gap,cap", [(1, 1200, 2), (3, 810, 3), (5, 420, 5), (7, 300, 7), (10, 120, 10)]
)
@pytest.mark.parametrize("chattiness,scale", [(0, 1.5), (0.5, 1), (1, 0.5)])
def test_level_interpolation(level, gap, cap, chattiness, scale):
    assert limits(level, chattiness) == (gap * scale, cap)


def make_director(crew, level=10, **kwargs):
    clock = Clock()
    director = Director(0.5, {}, clock, crew=crew, banter=level, desk=True, **kwargs)
    return director, clock


def test_budget_rolling_hour(crew):
    budget = Budget(1, crew)
    budget.consume(0)
    budget.consume(100)
    assert not budget.available(3599)
    assert budget.available(3600)
    budget.consume(3600)
    assert not budget.available(3699)
    assert budget.available(3700)


def test_replaces_idle_and_only_valid_scenes_charge(crew):
    director, clock = make_director(crew)
    clock.now = director.banter.gap
    assert director._take().kind == "banter"
    assert not director.banter.scenes
    assert director._take() is None
    director.banter_started()
    assert list(director.banter.scenes) == [clock.now]
    director.banter.cap = 1
    clock.now += 1000
    assert director._take() is None
    clock.now += 3600
    assert director._take().kind == "banter"


@pytest.mark.parametrize("speaker", ["player", "mira", "frieren"])
def test_any_speech_resets_quiet_gap(crew, speaker):
    director, clock = make_director(crew)
    clock.now = director.banter.gap - 1
    director.notify_spoke()  # shared notification for all three speaker paths
    clock.now += director.banter.gap - 1
    assert director._take() is None
    clock.now += 1
    assert director._take().kind == "banter"


@pytest.mark.parametrize("busy", ["user", "speaking", "reply"])
def test_busy_resets_quiet_gap(crew, busy):
    director, clock = make_director(crew)
    clock.now = 1000
    director.set_busy(busy, True)
    assert director._take() is None
    director.set_busy(busy, False)
    assert director._take() is None
    clock.now += director.banter.gap
    assert director._take().kind == "banter"


@pytest.mark.parametrize(
    "reason", ["pending action confirmation", "LLM model parked", "LLM quota near limit"]
)
def test_external_skip_reasons(crew, reason, caplog):
    director, clock = make_director(crew, banter_blocked=lambda: reason)
    clock.now = 1000
    with caplog.at_level("DEBUG"):
        assert director._take() is None
    assert reason in caplog.text
    assert not director.banter.scenes


@pytest.mark.parametrize("connected,paused", [(False, False), (True, True)])
def test_game_state_skip(crew, connected, paused):
    director, clock = make_director(crew)
    director.set_game_state(connected, paused)
    clock.now = 1000
    assert director._take() is None


@pytest.mark.parametrize("priority", ["critical", "notable", "ambient"])
def test_queued_items_reset_gap(crew, priority):
    director, clock = make_director(crew)
    clock.now = 1000
    director.submit(event("test", priority))
    assert director._take().kind == "test"
    assert director._take() is None
    clock.now += director.banter.gap
    assert director._take().kind == "banter"


@pytest.mark.parametrize("single,level", [(False, 0), (True, 10)])
def test_legacy_idle(single, level, crew):
    members = crew.members[:1] if single else crew.members
    director, clock = make_director(Crew(members, "mira"), level)
    clock.now = director._idle_seconds
    assert director.banter is None
    assert director._take().kind == "idle"


def test_cast_rotation_and_relationship_preference(crew):
    two = Crew(crew.members[:2], "mira")
    casting, rng = Casting(), random.Random(42)
    pairs = [casting.pick(two, rng) for _ in range(20)]
    assert all(a != b for a, b in zip(pairs, pairs[1:], strict=False))
    mira = replace(crew.carrier, chattiness=1, relationships={"frieren": "trusted friend"})
    others = [replace(member, chattiness=0) for member in crew.members[1:]]
    roster = Crew([mira, *others], "mira")
    samples = [Casting().pick(roster, rng) for _ in range(500)]
    assert all(first == "mira" for first, _ in samples)
    assert sum(second == "frieren" for _, second in samples) > 320


@pytest.mark.parametrize(
    "text",
    [
        "outsider: Hi.\nmira: Hello.",
        "mira: Hi.\nmira: Hello.",
        "mira: Hi.\nbroken interior\nfrieren: Hello.",
        "mira: [waves] Hi.\nfrieren: Hello.",
        "mira: <waves> Hi.\nfrieren: Hello.",
        "mira: Hi * there.\nfrieren: Hello.",
        "mira: (waves) Hi.\nfrieren: Hello.",
        "mira: Mira:\nfrieren: Hi.",
        "mira: Hello.",
    ],
)
def test_invalid_scene(text, crew):
    assert parse_scene(text, crew, "en") == []


def test_emphasis_is_unwrapped_not_rejected(crew):
    text = "mira: *Okh,* listen... *da*, **Bozhe moy**!\n**Frieren:** It is *loud*."
    assert parse_scene(text, crew, "en") == [
        ("mira", "Okh, listen... da, Bozhe moy!"),
        ("frieren", "It is loud."),
    ]


def test_parser_exterior_junk_prefixes_and_truncation(crew):
    text = (
        "Here is a scene:\nmira: Mirita: Hi.\nfrieren: フリーレン: Hello.\n"
        "mira: Yes.\nfrieren: Indeed.\nmira: Extra.\nThe end."
    )
    assert parse_scene(text, crew, "en") == [
        ("mira", "Hi."),
        ("frieren", "Hello."),
        ("mira", "Yes."),
        ("frieren", "Indeed."),
    ]


def chat(tmp_path, crew, output="mira: Mira: Hello.\nfrieren: Frieren: Hi.\nmira: Indeed."):
    result = Conversation(
        LLM([[LLMChunk(text=output)]]),
        crew.carrier,
        "es",
        TranscriptLog(tmp_path),
        crew=crew,
        context=lambda: "Fuel: 10 L",
        game="ats",
    )
    result.casting.pick = lambda *args: ("mira", "frieren")
    return result


async def test_one_tool_free_call_prompt_and_memory_provenance(tmp_path, crew):
    conversation = chat(tmp_path, crew)
    assert [line async for line in conversation.banter_scene()] == ["Hello.", "Hi.", "Indeed."]
    assert len(conversation.llm.requests) == 1
    _, system, tools = conversation.llm.requests[0]
    assert not tools
    assert "session language es" in system and "Fuel: 10 L" in system
    assert "Initiator: mira; partner: frieren" in system
    assert all(record.kind == "banter" and record.heard for record in conversation.records)
    records = [json.loads(line) for line in conversation.transcript.path.read_text().splitlines()]
    assert [row["speaker_id"] for row in records] == ["mira", "frieren", "mira"]
    assert all(row["kind"] == "banter" and row["source"] == "persona_claim" for row in records)
    assert "Hello." in conversation.transcript.dialogue()[0].text
    assert not any(
        "Hello." in message.text for message in conversation.transcript.dialogue(factual=True)
    )


async def test_unplayed_remainder_records_unheard(tmp_path, crew):
    conversation = chat(tmp_path, crew)
    conversation.playback_ack_required = True
    async with aclosing(conversation.banter_scene()) as source:
        assert await anext(source) == "Hello."
        conversation.acknowledge_playback()
        assert await anext(source) == "Hi."
    assert [record.heard for record in conversation.records] == [True, False, False]
    _, messages = DefaultHistoryRenderer().render(
        conversation.records, crew.members[1], crew, "en", None
    )
    assert not any(message.text == "Hi." for message in messages)


async def test_malformed_and_tools_do_not_charge(tmp_path, crew):
    conversation = chat(tmp_path, crew, "mira: Hi.")
    started = []
    conversation.banter_started = lambda: started.append(True)
    assert not [line async for line in conversation.banter_scene()]
    assert not started and not conversation.records
    conversation.llm = LLM([[LLMChunk(tool_call=ToolCall("x", "act_move", {}))]])
    assert not [line async for line in conversation.banter_scene()]
    assert not started


async def test_pending_confirmation_and_backend_budget_prevent_call(tmp_path, crew):
    conversation = chat(tmp_path, crew)
    conversation.actions = SimpleNamespace(pending=object())
    assert not [line async for line in conversation.banter_scene()]
    conversation.actions = None
    conversation.llm.banter_blocked = lambda: "quota"
    assert not [line async for line in conversation.banter_scene()]
    assert not conversation.llm.requests


@pytest.mark.parametrize("interrupt", ["ptt", "critical"])
async def test_voice_interrupts_scene_and_marks_remainder(tmp_path, crew, interrupt, monkeypatch):
    async def run_inline(function, *args):
        return function(*args)

    monkeypatch.setattr("familiar.voice.asyncio.to_thread", run_inline)
    conversation = chat(tmp_path, crew)
    playing = asyncio.Event()
    voices = []

    class TTS:
        name = "fake"

        async def synthesize(self, text, voice, language):
            voices.append(conversation.speaker.id)
            yield AudioChunk(b"00", 16000)

    class Player:
        def stop(self):
            pass

        async def play(self, chunks, pan=0.0):
            async for _ in chunks:
                pass

        async def wait_idle(self):
            if len(voices) == 1:
                return
            playing.set()
            await asyncio.Event().wait()

    class Recorder:
        def start(self):
            pass

    director, clock = make_director(crew)
    clock.now = 1000
    session = VoiceSession(
        None,
        Recorder(),
        Player(),
        None,
        TTS(),
        conversation,
        crew.carrier,
        "es",
        conversation.transcript,
        director=director,
    )
    session._interjection = event("banter", "ambient")
    session._reply_task = asyncio.create_task(session._interject(session._interjection))
    await asyncio.wait_for(playing.wait(), 1)
    if interrupt == "ptt":
        await session._press()
    if interrupt == "critical":
        director.submit(event("alert", "critical", alert_id="x", speak="Low fuel."))
        assert director._take().priority == "critical"
        # Exercise the same event consumer that admits critical alerts during speech.
        director.submit(event("alert", "critical", alert_id="y", speak="Low fuel."))
        consumer = asyncio.create_task(session._game_replies())
        await asyncio.sleep(0.02)
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await session._cancel_reply()
    assert [record.heard for record in conversation.records if record.kind == "banter"] == [
        True,
        False,
        False,
    ]
    assert voices[:2] == ["mira", "frieren"]


def test_gemini_parked_and_near_quota(monkeypatch):
    from familiar.backends.gemini.llm import GeminiLLM

    monkeypatch.setattr("familiar.backends.gemini.llm.time.monotonic", lambda: 1000)
    backend = GeminiLLM({"model": "configured", "quota_limits": {"configured": {"minute": 10}}})
    backend._requests.extend(("configured", 999) for _ in range(9))
    assert backend.banter_blocked() == "LLM quota near limit"
    backend._requests.clear()
    backend._parked[("another", True)] = 1001
    assert backend.banter_blocked() == "LLM model parked"
    backend._parked[("another", True)] = 999
    assert backend.banter_blocked() is None


@pytest.mark.parametrize(
    "level,single,has_game",
    [(5, False, False), (0, False, False), (5, True, False), (5, False, True)],
)
async def test_runtime_passes_session_budget_and_supports_desk(
    tmp_path, crew, monkeypatch, level, single, has_game
):
    import argparse
    from contextlib import AsyncExitStack

    from familiar import cli
    from familiar.config import AppConfig
    from familiar.session_settings import SessionSettings
    from test_m2_voice import FakeGameAdapter

    roster = Crew(crew.members[:1] if single else crew.members, "mira")
    backend = LLM([])
    monkeypatch.setattr(
        cli, "load", lambda kind, *args: {"games": FakeGameAdapter({}), "llm": backend}[kind]
    )
    config = AppConfig(
        memory={"enabled": False},
        flavor={"enabled": False},
        hands={"enabled": False},
        alerts={"enabled": False},
    )
    config.transcript_dir = tmp_path
    async with AsyncExitStack() as stack:
        selection = SessionSettings(
            crew=tuple(member.id for member in roster.members),
            carrier="mira",
            banter=level,
            language="en",
        )
        runtime = cli._Runtime(config, roster.carrier, "en", stack, roster, session=selection)
        conversation, adapter, director = runtime.conversation(
            argparse.Namespace(game="fake" if has_game else None)
        )
        if not has_game and (single or level == 0):
            assert director is None
            return
        assert director.banter is not None
        assert director._connected is (not has_game)
        conversation.banter_started()
        assert len(director.banter.scenes) == 1
        conversation.actions = SimpleNamespace(pending=object())
        assert director.banter_blocked() == "pending action confirmation"


async def test_scene_uses_recent_chronological_history_and_roles(tmp_path, crew):
    from familiar.history import HistoryRecord

    mira = replace(
        crew.carrier, roles={"ats": "the dispatch AI"}, relationships={"frieren": "trusted copilot"}
    )
    roster = Crew([mira, crew.members[1]], "mira")
    conversation = chat(tmp_path, roster)
    conversation.records = [
        HistoryRecord("player", "player", "A question"),
        HistoryRecord("frieren", "assistant", "A response"),
        HistoryRecord("player", "player", "Another question"),
    ]
    await consume_scene(conversation)
    system = conversation.llm.requests[0][1]
    assert (
        system.index("Player: A question")
        < system.index("Frieren: A response")
        < system.index("Player: Another question")
    )
    assert "the dispatch AI" in system and "trusted copilot" in system


async def test_banter_never_enters_factual_summary(tmp_path, crew):
    from familiar.memory.summary import summarize

    conversation = chat(tmp_path, crew)
    await consume_scene(conversation)
    conversation.transcript.record("user", text="I saw a mountain.", source="player_observed")
    llm = LLM([[LLMChunk(text="A mountain was seen.")]])
    await summarize(llm, conversation.transcript)
    prompt = llm.requests[0][0][0].text
    assert "I saw a mountain." in prompt
    assert "Hello." not in prompt and "Indeed." not in prompt


async def consume_scene(conversation):
    return [line async for line in conversation.banter_scene()]


def test_banter_events_cannot_be_observed_as_facts(store):
    from test_memory_keeper import keeper

    memory, _ = keeper(store)
    memory.observe(event("banter", "ambient", memory_salience=1))
    assert store.db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0


def test_raced_scene_is_dropped_instead_of_queued(crew):
    director, clock = make_director(crew)
    clock.now = 1000
    scene = director._take()
    director.set_busy("user", True)
    director.submit(scene)
    director.set_busy("user", False)
    assert not director._queue
    assert director._take() is None
    clock.now += director.banter.gap
    assert director._take().kind == "banter"


@pytest.mark.parametrize(
    "output", ["mira: Hello.\nmegumin: Hi.", "mira: Hello.\nfrieren: Hi.\nmegumin: A.\nmegumin: B."]
)
async def test_cast_members_required_and_third_member_limited(tmp_path, crew, output):
    conversation = chat(tmp_path, crew, output)
    assert not await consume_scene(conversation)
    assert not conversation.records


def test_budget_uses_mean_crew_chattiness(crew):
    roster = Crew(
        [replace(crew.members[0], chattiness=0), replace(crew.members[1], chattiness=0.5)], "mira"
    )
    assert Budget(5, roster).gap == 420 * 1.25


async def test_player_and_crew_voice_turn_reset_gap(tmp_path, crew):
    from familiar.interfaces import AudioClip
    from test_m2_voice import STT, TTS, Player

    conversation = chat(tmp_path, crew)
    conversation.language = "en"
    conversation.llm = LLM([[LLMChunk(text="An answer.")]])
    director, clock = make_director(crew)
    stt = STT()
    stt.release.set()
    session = VoiceSession(
        None,
        None,
        Player(),
        stt,
        TTS(),
        conversation,
        crew.carrier,
        "en",
        conversation.transcript,
        director=director,
    )
    clock.now = 1000
    await session._reply(AudioClip(b"00", 16000), 0)
    assert conversation.records[-1].heard
    clock.now += director.banter.gap - 0.1
    assert director._take() is None
    clock.now += 0.1
    assert director._take().kind == "banter"


def test_expired_queue_quiet_gap_starts_at_deadline(crew):
    director, clock = make_director(crew)
    director.submit(event("notable", "notable", ttl_s=10))
    clock.now = 10 + director.banter.gap
    assert director._take().kind == "banter"


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("Mira: Hi.\nFrieren: Hello.", "en"),
        ("**Mira:** Hi.\n**Frieren:** Hello.", "en"),
        ("ミラ：Hi.\nフリーレン：Hello.", "ja"),
        ("Here is a scene:\nMIRA: Hi.\nfrieren: Hello.", "en"),
    ],
)
def test_parser_accepts_names_aliases_markdown_and_fullwidth_colon(text, language, crew):
    assert parse_scene(text, crew, language) == [("mira", "Hi."), ("frieren", "Hello.")]
