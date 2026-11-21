"""Independently scheduled desk inputs."""

from datetime import datetime
from typing import Protocol


class Source(Protocol):
    name: str
    interval_s: float

    async def sample(self, now: datetime) -> tuple[dict, list[dict]]: ...
