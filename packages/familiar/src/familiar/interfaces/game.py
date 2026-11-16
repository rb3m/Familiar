"""GameAdapter plugin contract.

Events
    Each GameEvent's ``data`` carries:
    - ``summary`` (str, required): one English sentence for the LLM, units already formatted,
      e.g. "Entered Idaho from Oregon on I-84."
    - ``facts`` (list[str], optional): grounded facts the companion may use (landmarks, lore).
    - ``ttl_s`` (float, optional): seconds after which the event is stale and must not be
      spoken; the Director applies a per-priority default when absent.
    - any other kind-specific fields (amounts, names) for logging.

Snapshot
    ``snapshot()`` returns a dict that always has ``connected``, ``paused`` and ``driving``
    (bools) plus game-specific fields.

Situation
    ``describe()`` returns a short multi-line text of the current situation with exact,
    unit-formatted numbers. It is added to the system prompt every turn so the companion can
    answer "how much fuel do we have?" truthfully. Empty string when the game is not connected.
"""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from .types import GameEvent, ToolSpec


@runtime_checkable
class GameAdapter(Protocol):
    """Backend contract; aclose is an optional resource cleanup hook."""

    display_name: str  # Class metadata, available without connecting.
    icon: str  # Emoji; clients fall back to a game controller.
    name: str
    api_version: str
    capabilities: frozenset[str]
    game: str  # persona game_hooks key, chosen by the game pack

    def __init__(self, settings: dict) -> None: ...

    def events(self) -> AsyncIterator[GameEvent]: ...

    async def snapshot(self) -> dict: ...

    def describe(self) -> str: ...

    def tools(self) -> list[ToolSpec]: ...

    async def aclose(self) -> None: ...
