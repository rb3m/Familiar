"""Optional game actions and core-owned keyboard injection (API 2.0 additive)."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

ACTION_STATUSES = ("done", "already", "unavailable", "unknown", "error")


@dataclass(frozen=True)
class ActionSpec:
    description: str
    parameters: dict
    risky: bool = False
    risky_when: Callable[[dict], bool] | None = None
    timeout_s: float | None = None
    read_only: bool = False


@runtime_checkable
class Actuator(Protocol):
    """Results contain status and detail; optional action_context() returns str | None.

    Core reads that optional, fast context identity via getattr before confirmation.
    """

    def actions(self) -> dict[str, ActionSpec]: ...

    async def perform(self, name: str, arguments: dict) -> dict: ...


@runtime_checkable
class Keyboard(Protocol):
    """Lowercase evdev names without KEY_ (o, space, leftctrl, f1, kp1).

    Unknown names raise ValueError. Chord keys release in reverse order.
    """

    async def tap(self, keys: Sequence[str], hold_s: float = 0.05) -> None: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class KeyboardUser(Protocol):
    def attach_keyboard(self, keyboard: Keyboard) -> None: ...
