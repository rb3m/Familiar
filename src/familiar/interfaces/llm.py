"""LLMProvider plugin contract."""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from .types import LLMChunk, Message, ToolSpec


@runtime_checkable
class LLMProvider(Protocol):
    """Backend contract; aclose is an optional resource cleanup hook."""

    name: str
    api_version: str
    capabilities: frozenset[str]

    def __init__(self, settings: dict) -> None: ...

    def stream(
        self, messages: list[Message], system: str, tools: list[ToolSpec]
    ) -> AsyncIterator[LLMChunk]: ...

    async def aclose(self) -> None: ...
