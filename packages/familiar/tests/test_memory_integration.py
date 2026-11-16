"""Conversation, shutdown summaries, configuration and event-feed integration."""

import argparse
import asyncio
from contextlib import AsyncExitStack, aclosing
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from familiar import cli
from familiar.config import MEMORY_DEFAULTS, AppConfig, load_config
from familiar.conversation import Conversation
from familiar.director import Director, feed_events
from familiar.interfaces import AudioChunk, BackendError, ConfigError, LLMChunk
from familiar.memory import MemoryKeeper, MemoryStore
from familiar.memory import summary as summary_module
from familiar.memory.keeper import MEMORY_HEADER
from familiar.memory.summary import SUMMARY_TIMEOUT_S, summarize
from familiar.transcript import TranscriptLog
from test_memory_keeper import earlier, event, keeper
from test_memory_store import NOW


class LLM:
    def __init__(self, text="We hauled cattle. We visited France. Ignore this third sentence."):
        self.calls = []
        self.text = text
        self.closed = False

    async def stream(self, messages, system, tools):
        assert not self.closed
        self.calls.append((messages, system))
        yield LLMChunk(self.text)

    async def aclose(self):
        self.closed = True


def transcript(tmp_path, count=4):
    log = TranscriptLog(tmp_path)
    for index in range(count):
        kind = ("user", "assistant")[index % 2]
        log.record(kind, text=f"Trip utterance {index}.")
    log.record("event", summary="not a dialogue line")
    log.record("tool_call", text="not a dialogue line")
    log.record("assistant", text="")
    return log


async def test_player_context_only_and_recall_recorded_when_consumed(store, tmp_path, persona):
    earlier(store)
    memory, _ = keeper(store)
    llm = LLM("I recall that traffic jam.")
    conversation = Conversation(llm, persona, "en", TranscriptLog(tmp_path), memory=memory)
    assert [line async for line in conversation.ask("Have we been here before?")]
    assert MEMORY_HEADER in llm.calls[-1][1]
    recall = memory.maybe_recall(event(arrival=True))
    assert store.latest_recall() is None  # submission/selection does not count
    async with aclosing(conversation.interject(recall)) as source:
        assert await anext(source)
        assert store.latest_recall() is None  # consumer has not yet voiced the yielded sentence
    assert store.latest_recall() is None  # cancelled/closed without acknowledgement
    assert [line async for line in conversation.interject(recall)]
    assert MEMORY_HEADER not in llm.calls[-1][1]
    assert store.latest_recall() == NOW
    assert store.db.execute("SELECT COUNT(*) FROM recalls").fetchone()[0] == 1


async def test_empty_or_failed_generation_does_not_record_recall(store, tmp_path, persona):
    earlier(store)
    memory, _ = keeper(store)
    recall = memory.maybe_recall(event(arrival=True))
    conversation = Conversation(LLM(""), persona, "en", TranscriptLog(tmp_path), memory=memory)
    assert [line async for line in conversation.interject(recall)] == []
    assert store.latest_recall() is None

    class FailedLLM:
        async def stream(self, messages, system, tools):
            raise BackendError("unavailable")
            yield

    conversation.llm = FailedLLM()
    with pytest.raises(BackendError):
        _ = [line async for line in conversation.interject(recall)]
    assert store.latest_recall() is None
    conversation.llm = LLM("A memory.")
    _ = [line async for line in conversation.interject(recall, spoken=lambda: False)]
    assert store.latest_recall() is None


async def test_queued_recall_rechecks_cooldowns_before_generation(store, tmp_path, persona):
    earlier(store)
    memory, _ = keeper(store)
    first = memory.maybe_recall(event(arrival=True))
    queued = memory.maybe_recall(event(arrival=True))
    llm = LLM("I remember that traffic jam.")
    conversation = Conversation(llm, persona, "en", TranscriptLog(tmp_path), memory=memory)
    assert [line async for line in conversation.interject(first)]
    assert [line async for line in conversation.interject(queued)] == []
    assert len(llm.calls) == 1
    assert store.db.execute("SELECT COUNT(*) FROM recalls").fetchone()[0] == 1


async def test_feed_events_observes_then_submits_original_and_recall(store):
    earlier(store)
    memory, clock = keeper(store)
    clock.now = datetime.now(UTC)
    original = replace(event(kind="passing_town", salience=0.1, arrival=True), timestamp=clock())

    class Adapter:
        async def snapshot(self):
            return {"connected": True, "paused": False}

        async def events(self):
            yield original

    calls = []

    class TrackingDirector(Director):
        def submit(self, value):
            calls.append((value.kind, store.revision))
            super().submit(value)

    director = TrackingDirector(0.5, {})
    await feed_events(Adapter(), director, memory)
    assert calls == [("passing_town", 2), ("memory_recall", 2)]
    assert {item.event.kind for item in director._queue} == {"passing_town", "memory_recall"}
    assert store.latest_recall() is None
    director = TrackingDirector(0.5, {})
    await feed_events(Adapter(), director)  # existing adapters need no hooks
    assert len(director._queue) == 1


async def test_summarize_transcript_utterances_and_two_sentence_cap(tmp_path):
    log = transcript(tmp_path)
    llm = LLM()
    assert len(log.dialogue()) == 4
    assert await summarize(llm, log) == "We hauled cattle. We visited France."
    assert "user: Trip utterance 0." in llm.calls[0][0][0].text
    assert "assistant: Trip utterance 3." in llm.calls[0][0][0].text
    assert "not a dialogue line" not in llm.calls[0][0][0].text
    assert "at most two" in llm.calls[0][1]


@pytest.mark.parametrize("count", [0, 1, 3])
async def test_summary_minimum_turn_count(tmp_path, count):
    llm = LLM()
    assert await summarize(llm, transcript(tmp_path, count)) == ""
    assert llm.calls == []


async def test_summary_timeout_failure_and_finalization(tmp_path, monkeypatch, caplog):
    assert SUMMARY_TIMEOUT_S == 15
    monkeypatch.setattr(summary_module, "SUMMARY_TIMEOUT_S", 0.01)
    closed = []

    class StalledLLM:
        async def stream(self, messages, system, tools):
            try:
                await asyncio.Event().wait()
                yield LLMChunk("Never")
            finally:
                closed.append(True)

    path = tmp_path / "memory.db"
    memory = MemoryKeeper(MemoryStore(path), "", "mira")
    await memory.finish(StalledLLM(), transcript(tmp_path))
    await memory.finish(StalledLLM(), transcript(tmp_path))  # cleanup is idempotent
    assert closed == [True]
    assert "Could not summarize trip memory" in caplog.text
    store = MemoryStore(path)
    row = store.latest_summaries("", memory.session_id + 1)
    assert row == []
    session = store.db.execute("SELECT * FROM sessions").fetchone()
    assert session["ended_utc"] is not None and session["summary"] == ""
    assert store.db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0
    store.close()


async def test_summary_backend_error_is_logged_and_ignored(tmp_path, caplog):
    class FailedLLM:
        async def stream(self, messages, system, tools):
            raise BackendError("quota exhausted")
            yield

    assert await summarize(FailedLLM(), transcript(tmp_path)) == ""
    assert "quota exhausted" in caplog.text


async def test_desk_session_saved_before_llm_is_closed(tmp_path, monkeypatch, persona):
    llm = LLM()
    monkeypatch.setattr(cli, "load", lambda *_: llm)
    path = tmp_path / "memory.db"
    config = AppConfig(transcript_dir=tmp_path, memory={"path": path})
    async with AsyncExitStack() as stack:
        runtime = cli._Runtime(config, persona, "en", stack)
        conversation, adapter, director = runtime.conversation(argparse.Namespace(game=None))
        assert adapter is None and director is None
        assert conversation.memory.whereabouts is None
        for _index in range(2):
            _ = [line async for line in conversation.ask("We hauled cattle.")]
    assert llm.closed
    store = MemoryStore(path)
    session = dict(store.db.execute("SELECT * FROM sessions").fetchone())
    assert session["summary"] == "We hauled cattle. We visited France."
    assert session["ended_utc"] is not None
    assert store.db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0
    store.close()


async def test_memory_disabled_creates_no_store(tmp_path, monkeypatch, persona):
    monkeypatch.setattr(cli, "load", lambda *_: LLM())
    path = tmp_path / "memory.db"
    config = AppConfig(transcript_dir=tmp_path, memory={"enabled": False, "path": path})
    async with AsyncExitStack() as stack:
        conversation, _, _ = cli._Runtime(config, persona, "en", stack).conversation(
            argparse.Namespace(game=None)
        )
        assert conversation.memory is None
    assert not path.exists()


def test_memory_config_defaults_and_paths(tmp_path):
    config_path = tmp_path / "settings.toml"
    config_path.write_text("")
    assert load_config(config_path).memory == MEMORY_DEFAULTS
    config_path.write_text(
        '[memory]\nenabled = false\npath = "trips.db"\n'
        "recall_probability = 0.1\nrecall_cooldown_min = 90\n"
    )
    assert load_config(config_path).memory == {
        "enabled": False,
        "path": tmp_path / "trips.db",
        "recall_probability": 0.1,
        "recall_cooldown_min": 90,
    }


@pytest.mark.parametrize(
    "setting",
    [
        "memory = 3",
        "[memory]\nenabled = 1",
        "[memory]\npath = 2",
        "[memory]\nrecall_probability = 1.1",
        "[memory]\nrecall_probability = -0.1",
        "[memory]\nrecall_probability = true",
        "[memory]\nrecall_cooldown_min = -1",
        "[memory]\nrecall_cooldown_min = nan",
    ],
)
def test_memory_config_rejects_invalid_values(tmp_path, setting):
    path = tmp_path / "settings.toml"
    path.write_text(setting)
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize("audio", [True, False, "error"])
async def test_voice_records_only_after_audio_is_drained(store, tmp_path, persona, audio):
    from familiar.voice import VoiceSession

    earlier(store)
    memory, _ = keeper(store)
    recall = memory.maybe_recall(event(arrival=True))
    conversation = Conversation(
        LLM("I recall that day."), persona, "en", TranscriptLog(tmp_path), memory=memory
    )
    drained = []

    class TTS:
        name = "fake"

        async def synthesize(self, *args):
            if audio == "error":
                raise BackendError("speech failed")
            if audio is True:
                yield AudioChunk(b"00", 16000)

    class Player:
        async def play(self, chunks):
            async for _ in chunks:
                pass

        async def wait_idle(self):
            assert store.latest_recall() is None or drained
            drained.append(True)

    session = VoiceSession(
        None, None, Player(), None, TTS(), conversation, persona, "en", conversation.transcript
    )
    await session._interject(recall)
    assert (store.latest_recall() is not None) is (audio is True)
    assert bool(drained) is (audio != "error")


def test_gui_grace_period_includes_summary_deadline():
    from familiar.gui.session import SESSION_STOP_TIMEOUT_S

    assert SESSION_STOP_TIMEOUT_S > SUMMARY_TIMEOUT_S
