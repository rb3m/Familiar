"""TextToSpeech plugin contract."""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from .types import AudioChunk


@runtime_checkable
class TextToSpeech(Protocol):
    """Backend contract; aclose is an optional resource cleanup hook."""

    name: str
    api_version: str
    capabilities: frozenset[str]

    def __init__(self, settings: dict) -> None: ...

    def synthesize(
        self, text: str, voice: str | None, language: str
    ) -> AsyncIterator[AudioChunk]: ...

    async def aclose(self) -> None: ...
