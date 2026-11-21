"""LLMProvider plugin contract."""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from .types import LLMChunk, Message, ToolSpec

# Optional capability (API 2.4): stream() accepts require_tool=True, meaning the reply must be a
# call to one of the given tools. Core passes it only to backends that declare this.
REQUIRE_TOOL = "require_tool"


@runtime_checkable
class LLMProvider(Protocol):
    """Backend contract; aclose is an optional resource cleanup hook."""

    name: str
    api_version: str
    capabilities: frozenset[str]

    def __init__(self, settings: dict) -> None: ...

    def stream(
        self, messages: list[Message], system: str, tools: list[ToolSpec]
    ) -> AsyncIterator[LLMChunk]:
        """With the REQUIRE_TOOL capability, also accept ``require_tool: bool = False``."""
        ...

    async def aclose(self) -> None: ...
