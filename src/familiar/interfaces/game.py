"""GameAdapter plugin contract."""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from .types import GameEvent, ToolSpec


@runtime_checkable
class GameAdapter(Protocol):
    """Backend contract; aclose is an optional resource cleanup hook."""

    name: str
    api_version: str
    capabilities: frozenset[str]

    def __init__(self, settings: dict) -> None: ...

    def events(self) -> AsyncIterator[GameEvent]: ...

    async def snapshot(self) -> dict: ...

    def tools(self) -> list[ToolSpec]: ...

    async def aclose(self) -> None: ...
