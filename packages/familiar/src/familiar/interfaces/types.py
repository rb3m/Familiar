"""Immutable values exchanged through plugin interfaces."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant", "system", "tool"]
    text: str
    tool_calls: tuple["ToolCall", ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class LLMChunk:
    text: str = ""
    tool_call: ToolCall | None = None


@dataclass(frozen=True)
class AudioClip:
    """A complete PCM16 mono recording."""

    pcm: bytes
    sample_rate: int


@dataclass(frozen=True)
class AudioChunk:
    """A streaming PCM16 mono audio fragment."""

    pcm: bytes
    sample_rate: int


@dataclass(frozen=True)
class GameEvent:
    kind: str
    priority: Literal["critical", "notable", "ambient"]
    data: dict
    timestamp: datetime
