"""Streaming, transcript persistence, history limits, and cancellation."""

import asyncio
import json

import pytest
from familiar.conversation import Conversation
from familiar.interfaces import LLMChunk
from familiar.transcript import TranscriptLog


class FakeLLM:
    async def stream(self, messages, system, tools):
        assert messages[-1].role == "user"
        assert "Always reply in English." in system
        yield LLMChunk(text="Hello")
        yield LLMChunk(text=". Next ")
        yield LLMChunk(text="sentence! ")
        yield LLMChunk(text="Tail")


def records(log):
    return [json.loads(line) for line in log.path.read_text().splitlines()]


async def test_stream_transcript_history(tmp_path, persona):
    log = TranscriptLog(tmp_path / "logs", "session1")
    assert not log.directory.exists()
    conversation = Conversation(FakeLLM(), persona, "en", log, history_limit=3)
    assert [sentence async for sentence in conversation.ask("Hi")] == [
        "Hello.",
        "Next sentence!",
        "Tail",
    ]
    events = records(log)
    assert [event["kind"] for event in events] == ["user", "assistant"]
    assert events[-1]["text"] == "Hello. Next sentence! Tail"
    assert events[0]["v"] == 1
    assert events[0]["ts"].endswith("+00:00")
    assert "interrupted" not in events[-1]
    assert log.path.name.endswith("-session1.jsonl")
    for _ in range(3):
        _ = [sentence async for sentence in conversation.ask("Again")]
    assert len(conversation.history) == 3
    assert conversation.history[-1].role == "assistant"


async def test_cancellation(tmp_path, persona):
    waiting = asyncio.Event()

    class BlockingLLM:
        async def stream(self, messages, system, tools):
            yield LLMChunk(text="Partial reply")
            waiting.set()
            await asyncio.Event().wait()

    log = TranscriptLog(tmp_path)
    conversation = Conversation(BlockingLLM(), persona, "en", log)

    async def consume():
        return [sentence async for sentence in conversation.ask("Hello")]

    task = asyncio.create_task(consume())
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert records(log)[-1]["interrupted"] is True
    assert records(log)[-1]["text"] == "Partial reply"
    assert conversation.history[-1].text == "Partial reply"
