"""Google GenAI asynchronous text streaming backend."""

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
        self.model = settings.get("model", "gemini-3.8-flash")
        self.temperature = settings.get("temperature", 0.8)
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
        self._client = genai.Client(api_key=key)
        return self._client

    async def stream(
        self, messages: list[Message], system: str, tools: list[ToolSpec]
    ) -> AsyncIterator[LLMChunk]:
        """Stream response deltas, translating vendor API errors."""
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=[tool_to_declaration(tool) for tool in tools])]
            if tools
            else None,
            temperature=self.temperature,
        )
        try:
            responses = await self._get_client().aio.models.generate_content_stream(
                model=self.model, contents=messages_to_contents(messages), config=config
            )
            async for response in responses:
                for chunk in response_to_chunks(response):
                    yield chunk
        except errors.APIError as exc:
            raise BackendError(
                f"Gemini request failed ({exc.code}): {exc.message or 'API error'}",
                retryable=exc.code in {429, 500, 503, 504},
            ) from exc

    async def aclose(self) -> None:
        """Release the async SDK client if a request created one."""
        if self._client is not None:
            await self._client.aio.aclose()
            self._client = None
