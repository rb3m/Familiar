"""Google GenAI asynchronous text streaming backend."""

import asyncio
import os
from collections.abc import AsyncIterator
from uuid import uuid4

from google import genai
from google.genai import errors, types

from familiar.interfaces import (
    PLUGIN_API_VERSION,
    BackendError,
    ConfigError,
    LLMChunk,
    Message,
    ToolCall,
    ToolSpec,
)

RETRYABLE_CODES = frozenset({429, 500, 503, 504})


def messages_to_contents(messages: list[Message]) -> list[types.Content]:
    """Translate chat history, leaving system instructions to request config."""
    roles = {"user": "user", "assistant": "model"}
    return [
        types.Content(role=roles[message.role], parts=[types.Part(text=message.text)])
        for message in messages
        if message.role != "system"
    ]


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
            LLMChunk(tool_call=ToolCall(call.id or uuid4().hex, call.name or "", call.args or {}))
        )
    return chunks


class GeminiLLM:
    name = "gemini"
    api_version = PLUGIN_API_VERSION
    capabilities = frozenset({"streaming", "tools"})

    def __init__(self, settings: dict) -> None:
        self.settings = {"api_key_env": "GEMINI_API_KEY", **settings}
        self.model = settings.get("model", "gemini-3.5-flash-lite")
        self.temperature = settings.get("temperature", 0.8)
        self.max_retries = int(settings.get("max_retries", 2))
        self.backoff = float(settings.get("backoff_seconds", 1.0))
        self.fallback_models = list(settings.get("fallback_models", ["gemini-3.8-flash"]))
        self.first_chunk_timeout = float(settings.get("first_chunk_timeout_seconds", 8.0))
        self.read_timeout = float(settings.get("timeout_seconds", 30.0))
        self._client: genai.Client | None = None

    def _get_client(self) -> genai.Client:
        if self._client is not None:
            return self._client
        variable = self.settings["api_key_env"]
        if not isinstance(variable, str) or not variable:
            raise ConfigError("Gemini api_key_env must name an environment variable")
        key = os.environ.get(variable)
        if not key:
            raise ConfigError(f"Environment variable {variable!r} is missing or empty")
        http_options = types.HttpOptions(timeout=int(self.read_timeout * 1000))
        self._client = genai.Client(api_key=key, http_options=http_options)
        return self._client

    def _attempts(self) -> list[tuple[str, float]]:
        """(model, delay before trying) for each attempt: retries, then fallback models."""
        retries = [(self.model, self.backoff * 2**n) for n in range(self.max_retries)]
        fallbacks = [(model, 0.0) for model in self.fallback_models]
        return [(self.model, 0.0), *retries, *fallbacks]

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
        for model, delay in self._attempts():
            await asyncio.sleep(delay)
            started = False
            try:
                async for chunk in self._stream_once(model, contents, config):
                    started = True
                    yield chunk
                return
            except BackendError as exc:
                if started or not exc.retryable:
                    raise
                last_error = exc
        raise last_error  # type: ignore[misc]

    async def _stream_once(
        self, model: str, contents: list[types.Content], config: types.GenerateContentConfig
    ) -> AsyncIterator[LLMChunk]:
        try:
            responses = await self._open_stream(model, contents, config)
            async for response in responses:
                for chunk in response_to_chunks(response):
                    yield chunk
        except errors.APIError as exc:
            raise BackendError(
                f"Gemini {model} failed ({exc.code}): {exc.message or 'API error'}",
                retryable=exc.code in RETRYABLE_CODES,
            ) from exc
        except TimeoutError as exc:
            raise BackendError(
                f"Gemini {model} sent nothing within {self.first_chunk_timeout:g}s",
                retryable=True,
            ) from exc

    async def _open_stream(
        self, model: str, contents: list[types.Content], config: types.GenerateContentConfig
    ) -> AsyncIterator[types.GenerateContentResponse]:
        """Open the stream and wait for its first response under the first-chunk deadline.

        An overloaded model can accept the request and then stall without an error, so the
        deadline turns that silence into a retryable failure instead of a frozen companion.
        """
        async with asyncio.timeout(self.first_chunk_timeout):
            stream = await self._get_client().aio.models.generate_content_stream(
                model=model, contents=contents, config=config
            )
            first = await anext(stream, None)
        return _prepend(first, stream)

    async def aclose(self) -> None:
        """Release the async SDK client if a request created one."""
        if self._client is not None:
            await self._client.aio.aclose()
            self._client = None


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
