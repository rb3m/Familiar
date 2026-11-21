"""Companion services run alongside games and desk conversations."""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .types import GameEvent, ToolSpec


@dataclass(frozen=True)
class ServiceTool:
    spec: ToolSpec
    handler: Callable[..., Awaitable[dict]]
    # Legacy spelling of role="controls".
    carrier_only: bool = False
    # May be offered on an unprompted turn when an event names it in data["tools"].
    autonomous: bool = False
    # Crew role that owns the tool: "controls" (game actions) or "dj" (music); None is everyone.
    role: str | None = None


@dataclass(frozen=True)
class SpeechState:
    listening: bool = False
    speaking: bool = False
    critical: bool = False
    event_kind: str | None = None


@runtime_checkable
class CompanionService(Protocol):
    name: str
    api_version: str
    capabilities: frozenset[str]

    def __init__(self, settings: dict) -> None: ...

    def events(self) -> AsyncIterator[GameEvent]: ...

    def service_tools(self) -> list[ServiceTool]: ...

    async def speech_state(self, state: SpeechState) -> None: ...

    def describe(self) -> str: ...

    async def aclose(self) -> None: ...
