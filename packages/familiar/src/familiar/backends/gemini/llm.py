"""Google GenAI asynchronous text streaming backend."""

import asyncio
import base64
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from uuid import uuid4

from google import genai
from google.genai import errors, types

from familiar.backends.models import model_defaults
from familiar.interfaces import (
    PLUGIN_API_VERSION,
    BackendError,
    LLMChunk,
    Message,
    ToolCall,
    ToolSpec,
)

from .client import make_client

_LOG = logging.getLogger(__name__)
QUOTA_PARK_S = 15 * 60  # a used-up daily quota won't come back within a session's retries


class QuotaExhaustedError(BackendError):
    """429 RESOURCE_EXHAUSTED for a quota, not a momentary rate spike."""


RETRYABLE_CODES = frozenset({429, 500, 503, 504})


def messages_to_contents(messages: list[Message]) -> list[types.Content]:
    """Translate chat history, leaving system instructions to request config."""
    contents = []
    names = {}
    for message in messages:
        if message.role == "system":
            continue
        if message.role == "tool":
            call_id = message.tool_call_id
            response = types.FunctionResponse(
                id=call_id, name=names[call_id], response=json.loads(message.text)
            )
            _append_response(contents, response)
            continue
        parts = [types.Part(text=message.text)] if message.text else []
        for call in message.tool_calls:
            names[call.id] = call.name
            signature = call.metadata.get("thought_signature")
            parts.append(
                types.Part(
                    function_call=types.FunctionCall(
                        id=call.id, name=call.name, args=call.arguments
                    ),
                    thought_signature=base64.b64decode(signature)
                    if signature is not None
                    else None,
                )
            )
        contents.append(
            types.Content(role={"user": "user", "assistant": "model"}[message.role], parts=parts)
        )
    return contents


def _append_response(contents: list[types.Content], response: types.FunctionResponse) -> None:
    """Parallel results belong to the same user turn."""
    part = types.Part(function_response=response)
    if contents and contents[-1].role == "user" and contents[-1].parts[0].function_response:
        contents[-1].parts.append(part)
        return
    contents.append(types.Content(role="user", parts=[part]))


def tool_to_declaration(tool: ToolSpec) -> types.FunctionDeclaration:
    """Preserve the tool's JSON schema in an SDK function declaration."""
    return types.FunctionDeclaration(
        name=tool.name, description=tool.description, parameters_json_schema=tool.parameters
    )


def response_to_chunks(response: types.GenerateContentResponse) -> list[LLMChunk]:
    """Extract text and tool calls from the first generated candidate."""
    if not response.candidates or response.candidates[0].content is None:
        return []
    parts = response.candidates[0].content.parts or []
    return [chunk for part in parts for chunk in _part_chunks(part)]


def _part_chunks(part: types.Part) -> list[LLMChunk]:
    chunks = []
    if part.text and not part.thought:
        chunks.append(LLMChunk(text=part.text))
    call = part.function_call
    if call is not None:
        chunks.append(
            LLMChunk(
                tool_call=ToolCall(
                    call.id or uuid4().hex,
                    call.name or "",
                    call.args or {},
                    {"thought_signature": base64.b64encode(part.thought_signature).decode("ascii")}
                    if part.thought_signature is not None
                    else {},
                )
            )
        )
    return chunks


class GeminiLLM:
    name = "gemini"
    api_version = PLUGIN_API_VERSION
    capabilities = frozenset({"streaming", "tools"})

    def __init__(self, settings: dict) -> None:
        self.settings = {"api_key_env": "GEMINI_API_KEY", **settings}
        defaults = model_defaults("gemini")
        self.model = settings.get("model", defaults["llm"])
        self.temperature = settings.get("temperature", 0.8)
        self.max_retries = int(settings.get("max_retries", 2))
        self.backoff = float(settings.get("backoff_seconds", 1.0))
        self.fallback_models = list(settings.get("fallback_models", defaults["fallback_models"]))
        self.first_chunk_timeout = float(settings.get("first_chunk_timeout_seconds", 8.0))
        self.read_timeout = float(settings.get("timeout_seconds", 30.0))
        self._client: genai.Client | None = None
        self._fallback_client: genai.Client | None = None
        self._fallback_settings = self._fallback_key_settings(settings)
        self._parked: dict[tuple[str, bool], float] = {}  # (model, fallback) -> retry after

    def _fallback_key_settings(self, settings: dict) -> dict | None:
        """Fallback models may use their own key (e.g. a capped paid key behind a free one)."""
        variable = settings.get("fallback_api_key_env")
        if not variable:
            return None
        if not os.environ.get(variable):
            _LOG.warning(
                "%s is unset; Gemini fallback models use %s", variable, self.settings["api_key_env"]
            )
            return None
        return {**self.settings, "api_key_env": variable}

    def _get_client(self, fallback: bool = False) -> genai.Client:
        if fallback and self._fallback_settings is not None:
            if self._fallback_client is None:
                self._fallback_client = make_client(self._fallback_settings, api_version=None)
            return self._fallback_client
        if self._client is not None:
            return self._client
        self._client = make_client(self.settings, api_version=None)
        return self._client

    def _attempts(self) -> list[tuple[str, float, bool]]:
        """(model, delay before trying, is fallback) per attempt: retries, then fallbacks."""
        retries = [(self.model, self.backoff * 2**n, False) for n in range(self.max_retries)]
        fallbacks = [(model, 0.0, True) for model in self.fallback_models]
        return [(self.model, 0.0, False), *retries, *fallbacks]

    async def stream(
        self, messages: list[Message], system: str, tools: list[ToolSpec]
    ) -> AsyncIterator[LLMChunk]:
        """Stream response deltas, retrying busy/rate-limit errors before the first chunk."""
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=[tool_to_declaration(tool) for tool in tools])]
            if tools
            else None,
            temperature=self.temperature,
        )
        contents = messages_to_contents(messages)
        last_error: BackendError | None = None
        for model, delay, fallback in self._attempts():
            if self._parked.get((model, fallback), 0) > time.monotonic():
                continue
            await asyncio.sleep(delay)
            started = False
            try:
                async for chunk in self._stream_once(model, contents, config, fallback):
                    started = True
                    yield chunk
                return
            except QuotaExhaustedError as exc:
                _LOG.warning("%s; skipping it for %d min", exc, QUOTA_PARK_S // 60)
                self._parked[model, fallback] = time.monotonic() + QUOTA_PARK_S
                last_error = exc
            except BackendError as exc:
                if started or not exc.retryable:
                    raise
                last_error = exc
        if last_error is None:
            raise BackendError("Gemini: every model is parked after quota errors", retryable=True)
        raise last_error

    async def _stream_once(
        self,
        model: str,
        contents: list[types.Content],
        config: types.GenerateContentConfig,
        fallback: bool = False,
    ) -> AsyncIterator[LLMChunk]:
        try:
            responses = await self._open_stream(model, contents, config, fallback)
            async for response in responses:
                for chunk in response_to_chunks(response):
                    yield chunk
        except errors.APIError as exc:
            message = f"Gemini {model} failed ({exc.code}): {exc.message or 'API error'}"
            if exc.code == 429 and "quota" in (exc.message or "").lower():
                raise QuotaExhaustedError(message, retryable=True) from exc
            raise BackendError(message, retryable=exc.code in RETRYABLE_CODES) from exc
        except TimeoutError as exc:
            raise BackendError(
                f"Gemini {model} sent nothing within {self.first_chunk_timeout:g}s",
                retryable=True,
            ) from exc

    async def _open_stream(
        self,
        model: str,
        contents: list[types.Content],
        config: types.GenerateContentConfig,
        fallback: bool = False,
    ) -> AsyncIterator[types.GenerateContentResponse]:
        """Open the stream and wait for its first response under the first-chunk deadline.

        An overloaded model can accept the request and then stall without an error, so the
        deadline turns that silence into a retryable failure instead of a frozen companion.
        """
        async with asyncio.timeout(self.first_chunk_timeout):
            stream = await self._get_client(fallback).aio.models.generate_content_stream(
                model=model, contents=contents, config=config
            )
            first = await anext(stream, None)
        return _prepend(first, stream)

    async def aclose(self) -> None:
        """Release the async SDK client if a request created one."""
        clients = (self._client, self._fallback_client)
        self._client = self._fallback_client = None
        for client in clients:
            if client is not None:
                await client.aio.aclose()


async def _prepend(
    first: types.GenerateContentResponse | None,
    rest: AsyncIterator[types.GenerateContentResponse],
) -> AsyncIterator[types.GenerateContentResponse]:
    """Yield an already-received first response, then the remainder of the stream."""
    if first is None:
        return
    yield first
    async for response in rest:
        yield response
