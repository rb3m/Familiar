"""Contract for independently installed CLI commands."""

from argparse import ArgumentParser, Namespace
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from familiar.config import AppConfig


class Command(Protocol):
    name: str
    api_version: str  # PLUGIN_API_VERSION; major version must match core.
    help: str

    def add_arguments(self, parser: ArgumentParser) -> None: ...

    def run(self, args: Namespace, config: "AppConfig") -> int | Awaitable[int]: ...
