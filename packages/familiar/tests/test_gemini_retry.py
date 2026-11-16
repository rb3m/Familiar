"""Retry and fallback behaviour of GeminiLLM, with the network call faked."""

import pytest
from familiar.backends.gemini.llm import GeminiLLM
from familiar.interfaces import BackendError, LLMChunk


def _llm(script: dict[str, list]) -> tuple[GeminiLLM, list[str]]:
    """Each model name maps to a queue of outcomes: an exception or a list of texts."""
    llm = GeminiLLM({"model": "main", "fallback_models": ["lite"], "backoff_seconds": 0})
    calls: list[str] = []

    async def fake_once(model, contents, config):
        calls.append(model)
        outcome = script[model].pop(0)
        for text in outcome.get("texts", []):
            yield LLMChunk(text=text)
        if "error" in outcome:
            raise outcome["error"]

    llm._stream_once = fake_once
    return llm, calls


async def _collect(llm: GeminiLLM) -> str:
    return "".join([chunk.text async for chunk in llm.stream([], "sys", [])])


busy = {"error": BackendError("busy", retryable=True)}


async def test_retries_busy_then_succeeds():
    llm, calls = _llm({"main": [busy, {"texts": ["hi"]}]})
    assert await _collect(llm) == "hi"
    assert calls == ["main", "main"]


async def test_falls_back_after_retries_exhausted():
    llm, calls = _llm({"main": [busy, busy, busy], "lite": [{"texts": ["ok"]}]})
    assert await _collect(llm) == "ok"
    assert calls == ["main", "main", "main", "lite"]


async def test_non_retryable_error_raises_immediately():
    llm, calls = _llm({"main": [{"error": BackendError("bad key", retryable=False)}]})
    with pytest.raises(BackendError, match="bad key"):
        await _collect(llm)
    assert calls == ["main"]


async def test_no_retry_once_output_started():
    llm, calls = _llm({"main": [{"texts": ["par"], "error": BackendError("x", retryable=True)}]})
    with pytest.raises(BackendError):
        await _collect(llm)
    assert calls == ["main"]


async def test_all_attempts_fail_raises_last_error():
    llm, _ = _llm({"main": [busy, busy, busy], "lite": [busy]})
    with pytest.raises(BackendError, match="busy"):
        await _collect(llm)


async def test_silent_stall_times_out_and_falls_back(monkeypatch):
    """A model that accepts the request but never sends anything must not hang the companion."""
    import asyncio

    llm = GeminiLLM(
        {"max_retries": 0, "fallback_models": ["lite"], "first_chunk_timeout_seconds": 0.05}
    )

    class FakeModels:
        async def generate_content_stream(self, model, contents, config):
            async def stalled():
                await asyncio.sleep(10)
                yield None

            async def answers():
                from google.genai import types

                yield types.GenerateContentResponse(
                    candidates=[
                        types.Candidate(
                            content=types.Content(role="model", parts=[types.Part(text="ok")])
                        )
                    ]
                )

            return answers() if model == "lite" else stalled()

    class FakeClient:
        class aio:  # noqa: N801
            models = FakeModels()

    monkeypatch.setattr(llm, "_get_client", lambda: FakeClient)
    assert await _collect(llm) == "ok"
