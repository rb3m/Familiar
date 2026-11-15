"""SpeechToText plugin contract."""

from typing import Protocol, runtime_checkable

from .types import AudioClip


@runtime_checkable
class SpeechToText(Protocol):
    """Backend contract; aclose is an optional resource cleanup hook."""

    name: str
    api_version: str
    capabilities: frozenset[str]

    def __init__(self, settings: dict) -> None: ...

    async def transcribe(self, audio: AudioClip, language: str) -> str: ...

    async def aclose(self) -> None: ...
