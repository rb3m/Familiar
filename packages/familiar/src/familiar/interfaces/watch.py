"""Optional telemetry quantities; values and thresholds use SI units."""

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable


@dataclass(frozen=True)
class WatchSpec:
    unit: Literal["m", "m/s", "s", "%", "count"]
    description: str


@dataclass(frozen=True)
class WatchSample:
    values: dict[str, float | None]
    context: str | None
    valid: bool
    predictable: bool
    timestamp: float  # Monotonic acquisition time, not the time it was requested.


@runtime_checkable
class Watchable(Protocol):
    def watchables(self) -> dict[str, WatchSpec]: ...

    def watch_sample(self) -> WatchSample: ...
