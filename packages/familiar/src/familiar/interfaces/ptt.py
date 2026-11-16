"""Push-to-talk source contract: True on press, False on release."""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable


@runtime_checkable
class PushToTalk(Protocol):
    name: str
    api_version: str
    capabilities: frozenset[str]

    def __init__(self, settings: dict) -> None: ...

    def events(self) -> AsyncIterator[bool]: ...

    async def aclose(self) -> None: ...
