"""Tool round trips, bounded execution and intact history groups."""

import asyncio
import base64
import json
from contextlib import aclosing
from dataclasses import asdict
from datetime import UTC, datetime

import pytest
from familiar.backends.gemini.llm import messages_to_contents, response_to_chunks
from familiar.conversation import Conversation
from familiar.interfaces import GameEvent, LLMChunk, Message, ToolCall, ToolSpec
from familiar.tools import ToolRegistry
from familiar.transcript import TranscriptLog
from google.genai import types

SPEC = ToolSpec(
    "lookup",
    "Look up a value",
    {
        "type": "object",
        "properties": {"value": {"type": "number"}},
        "required": ["value"],
        "additionalProperties": False,
    },
)


def test_gemini_tool_round_trip():
    response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(text="Checking."),
                        types.Part(
                            function_call=types.FunctionCall(
                                id="a", name="lookup", args={"value": 4}
                            ),
                            thought_signature=b"opaque\x00\xff",
                        ),
                        types.Part(function_call=types.FunctionCall(id="b", name="other", args={})),
                    ],
                )
            )
        ]
    )
    chunks = response_to_chunks(response)
    calls = tuple(chunk.tool_call for chunk in chunks if chunk.tool_call is not None)
    # Metadata is also safe to record in the JSONL transcript.
    assert json.loads(json.dumps(asdict(calls[0])))["metadata"] == {
        "thought_signature": base64.b64encode(b"opaque\x00\xff").decode("ascii")
    }
    history = [
        Message("user", "Check"),
        Message("assistant", "Checking.", calls),
        Message("tool", '{"answer":4}', tool_call_id="a"),
        Message("tool", '{"error":"unavailable"}', tool_call_id="b"),
    ]
    contents = messages_to_contents(history)
    assert [item.role for item in contents] == ["user", "model", "user"]
    parts = contents[1].parts
    assert parts[0].text == "Checking."
    assert parts[1].thought_signature == b"opaque\x00\xff"
    assert parts[1].function_call == response.candidates[0].content.parts[1].function_call
    results = contents[2].parts
    assert [(p.function_response.id, p.function_response.name) for p in results] == [
        ("a", "lookup"),
        ("b", "other"),
    ]
    assert results[0].function_response.response == {"answer": 4}
    assert Message(**asdict(Message("user", "Hello"))) == Message("user", "Hello")


class LLM:
    def __init__(self, rounds):
        self.rounds = iter(rounds)
        self.requests = []

    async def stream(self, messages, system, tools):
        self.requests.append((messages, system, tools))
        for chunk in next(self.rounds):
            yield chunk


def registry(handler=None):
    async def lookup(value):
        return {"answer": value * 2}

    tools = ToolRegistry()
    tools.register(SPEC, handler or lookup)
    return tools


def assert_complete(history):
    for index, message in enumerate(history):
        if message.role == "tool":
            assert any(
                call.id == message.tool_call_id
                for earlier in history[:index]
                for call in earlier.tool_calls
            )
        if message.tool_calls:
            results = history[index + 1 : index + 1 + len(message.tool_calls)]
            assert [item.tool_call_id for item in results] == [c.id for c in message.tool_calls]


async def test_text_and_multiple_tool_rounds(tmp_path, persona):
    a = ToolCall("a", "lookup", {"value": 4})
    b = ToolCall("b", "lookup", {"value": 8})
    llm = LLM(
        [
            [LLMChunk(text="Checking. "), LLMChunk(tool_call=a)],
            [LLMChunk(tool_call=b)],
            [LLMChunk(text="Sixteen.")],
        ]
    )
    log = TranscriptLog(tmp_path)
    conversation = Conversation(llm, persona, "en", log, tools=registry())
    assert [s async for s in conversation.ask("Look up four twice")] == ["Checking.", "Sixteen."]
    assert len(llm.requests) == 3
    assert all(request[2] == [SPEC] for request in llm.requests)
    assert json.loads(llm.requests[1][0][-1].text) == {"answer": 8}
    assert json.loads(llm.requests[2][0][-1].text) == {"answer": 16}
    assert conversation.history[1] == Message("assistant", "Checking. ", (a,))
    assert_complete(conversation.history)
    records = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert len([r for r in records if r["kind"] == "tool_call"]) == 2
    assert [json.loads(r["text"]) for r in records if r["kind"] == "tool_result"] == [
        {"answer": 8},
        {"answer": 16},
    ]


async def test_tool_round_cap_final_call_and_history_trimming(tmp_path, persona):
    llm = LLM(
        [[LLMChunk(tool_call=ToolCall(str(i), "lookup", {"value": i}))] for i in range(3)]
        + [[LLMChunk(text="Done.")]]
    )
    conversation = Conversation(
        llm, persona, "en", TranscriptLog(tmp_path), tools=registry(), history_limit=1
    )
    assert [s async for s in conversation.ask("Look")] == ["Done."]
    assert [len(request[2]) for request in llm.requests] == [1, 1, 1, 0]
    for history, _, _ in llm.requests:
        assert_complete(history)
    assert_complete(conversation.history)
    # The limit never splits the current turn, so its question is kept with its calls.
    assert conversation.history[0].role == "user"
    assert len(conversation.history) == 8


async def test_trimmed_history_never_opens_with_a_tool_call(tmp_path, persona):
    """Live 400 (2026-11-17): the cut fell between a question and its call."""
    llm = LLM(
        [
            [LLMChunk(tool_call=ToolCall("a", "lookup", {"value": 1}))],
            [LLMChunk(text="Two.")],
            [LLMChunk(text="Hello.")],
        ]
    )
    conversation = Conversation(
        llm, persona, "en", TranscriptLog(tmp_path), tools=registry(), history_limit=4
    )
    assert [s async for s in conversation.ask("Look")] == ["Two."]
    assert [s async for s in conversation.ask("Hi")] == ["Hello."]
    for history, _, _ in llm.requests:
        assert history[0].role == "user"
        assert_complete(history)
    assert [m.role for m in conversation.history] == ["user", "assistant"]


@pytest.mark.parametrize(
    "arguments",
    [{}, {"value": "four"}, {"value": True}, {"value": float("nan")}, {"value": 4, "extra": 2}, []],
)
async def test_invalid_tool_arguments(arguments):
    assert "error" in await registry().execute(ToolCall("a", "lookup", arguments))


async def test_errors_unknown_tools_and_interjections(tmp_path, persona):
    async def fail(value):
        raise RuntimeError("lookup is offline")

    tools = registry(fail)
    assert await tools.execute(ToolCall("x", "unknown", {})) == {"error": "Unknown tool: unknown"}
    llm = LLM(
        [
            [LLMChunk(tool_call=ToolCall("a", "lookup", {"value": 1}))],
            [LLMChunk(text="It failed.")],
            [LLMChunk(text="Hello.")],
        ]
    )
    conversation = Conversation(llm, persona, "en", TranscriptLog(tmp_path), tools=tools)
    assert [s async for s in conversation.ask("Look")] == ["It failed."]
    assert json.loads(llm.requests[1][0][-1].text) == {"error": "lookup is offline"}
    event = GameEvent("test", "critical", {"summary": "Hello"}, datetime.now(UTC))
    assert [s async for s in conversation.interject(event)] == ["Hello."]
    assert llm.requests[-1][2] == []


async def test_tools_not_executed_when_not_offered(tmp_path, persona):
    executed = []

    async def handler(value):
        executed.append(value)
        return {}

    llm = LLM([[LLMChunk(tool_call=ToolCall("a", "lookup", {"value": 1}))]])
    conversation = Conversation(
        llm, persona, "en", TranscriptLog(tmp_path), tools=registry(handler)
    )
    event = GameEvent("test", "critical", {"summary": "Hello"}, datetime.now(UTC))
    assert [s async for s in conversation.interject(event)] == []
    assert not executed
    assert_complete(conversation.history)


async def test_cancellation_during_handler_keeps_complete_group(tmp_path, persona):
    waiting = asyncio.Event()

    async def handler(value):
        waiting.set()
        await asyncio.Event().wait()

    llm = LLM(
        [
            [
                LLMChunk(tool_call=ToolCall("a", "lookup", {"value": 1})),
                LLMChunk(tool_call=ToolCall("b", "lookup", {"value": 2})),
            ]
        ]
    )
    conversation = Conversation(
        llm, persona, "en", TranscriptLog(tmp_path), tools=registry(handler)
    )

    async def consume():
        async with aclosing(conversation.ask("Look")) as sentences:
            return [s async for s in sentences]

    task = asyncio.create_task(consume())
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert_complete(conversation.history)
    assert len(conversation.history) == 4


async def test_backend_failure_after_call_keeps_group(tmp_path, persona):
    from familiar.interfaces import BackendError

    class FailingLLM:
        async def stream(self, messages, system, tools):
            yield LLMChunk(tool_call=ToolCall("a", "lookup", {"value": 1}))
            raise BackendError("stream failed")

    conversation = Conversation(
        FailingLLM(), persona, "en", TranscriptLog(tmp_path), tools=registry()
    )
    with pytest.raises(BackendError):
        _ = [s async for s in conversation.ask("Look")]
    assert_complete(conversation.history)
    assert json.loads(conversation.history[-1].text).get("error")


async def test_parallel_calls_and_final_disabled_call(tmp_path, persona):
    executed = []

    async def handler(value):
        executed.append(value)
        return {"value": value}

    rounds = [
        [
            LLMChunk(tool_call=ToolCall("a", "lookup", {"value": 1})),
            LLMChunk(tool_call=ToolCall("b", "lookup", {"value": "bad"})),
        ],
        [LLMChunk(tool_call=ToolCall("c", "lookup", {"value": 2}))],
        [LLMChunk(tool_call=ToolCall("d", "lookup", {"value": 3}))],
        [LLMChunk(tool_call=ToolCall("e", "lookup", {"value": 4}))],
    ]
    llm = LLM(rounds)
    conversation = Conversation(
        llm, persona, "en", TranscriptLog(tmp_path), tools=registry(handler)
    )
    assert [s async for s in conversation.ask("Look")] == []
    assert executed == [1, 2, 3]
    assert len(llm.requests) == 4
    assert llm.requests[-1][2] == []
    assert "unavailable" in json.loads(conversation.history[-1].text)["error"]
    assert_complete(conversation.history)
