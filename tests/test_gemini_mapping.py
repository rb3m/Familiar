"""Exercise SDK values and streaming without creating network connections."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai import errors, types

from familiar.backends.gemini.llm import (
    GeminiLLM,
    messages_to_contents,
    response_to_chunks,
    tool_to_declaration,
)
from familiar.interfaces import BackendError, LLMChunk, LLMProvider, Message, ToolSpec


def test_messages():
    contents = messages_to_contents(
        [
            Message("system", "system"),
            Message("user", "hello"),
            Message("assistant", "hi"),
        ]
    )
    assert [content.role for content in contents] == ["user", "model"]
    assert [content.parts[0].text for content in contents] == ["hello", "hi"]


def test_tool():
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    declaration = tool_to_declaration(ToolSpec("look", "Look around", schema))
    assert isinstance(declaration, types.FunctionDeclaration)
    assert declaration.name == "look"
    assert declaration.description == "Look around"
    assert declaration.parameters_json_schema == schema


def test_response():
    response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    parts=[
                        types.Part(text="Hello"),
                        types.Part(
                            function_call=types.FunctionCall(
                                id="id-1",
                                name="look",
                                args={"name": "road"},
                            )
                        ),
                        types.Part(text="private reasoning", thought=True),
                    ],
                )
            )
        ]
    )
    chunks = response_to_chunks(response)
    assert chunks[0] == LLMChunk(text="Hello")
    assert chunks[1].tool_call.id == "id-1"
    assert chunks[1].tool_call.name == "look"
    assert chunks[1].tool_call.arguments == {"name": "road"}
    assert len(chunks) == 2
    assert response_to_chunks(types.GenerateContentResponse()) == []
    assert response_to_chunks(types.GenerateContentResponse(candidates=[types.Candidate()])) == []


def test_construct_without_secret(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    llm = GeminiLLM({})
    assert llm._client is None
    assert llm.model == "gemini-3.8-flash"
    assert isinstance(llm, LLMProvider)


async def test_stream_sdk_contract():
    response = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(parts=[types.Part(text="Hello")]),
            )
        ]
    )

    async def responses():
        yield response

    generate = AsyncMock(return_value=responses())
    close = AsyncMock()
    llm = GeminiLLM({"model": "offline-model", "temperature": 0.3})
    llm._client = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(generate_content_stream=generate),
            aclose=close,
        )
    )
    tool = ToolSpec("look", "Look", {"type": "object"})
    assert [chunk async for chunk in llm.stream([Message("user", "hi")], "system", [tool])] == [
        LLMChunk(text="Hello"),
    ]
    kwargs = generate.call_args.kwargs
    assert kwargs["model"] == "offline-model"
    assert kwargs["config"].system_instruction == "system"
    assert kwargs["config"].temperature == 0.3
    assert kwargs["config"].tools[0].function_declarations[0].name == "look"
    await llm.aclose()
    close.assert_awaited_once()
    assert llm._client is None


@pytest.mark.parametrize(
    "code, retryable",
    [(429, True), (500, True), (503, True), (504, True), (400, False), (403, False)],
)
async def test_api_error(code, retryable):
    generate = AsyncMock(side_effect=errors.APIError(code, {"message": "offline failure"}))
    llm = GeminiLLM({})
    llm._client = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(generate_content_stream=generate),
        )
    )
    with pytest.raises(BackendError) as caught:
        _ = [chunk async for chunk in llm.stream([], "system", [])]
    assert caught.value.retryable is retryable
