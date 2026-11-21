"""Independently scheduled desk inputs, chosen by name in `[desk] sources`."""

from datetime import datetime
from importlib.metadata import entry_points
from typing import Protocol

from familiar.interfaces import ConfigError

SOURCE_GROUP = "familiar_desk.sources"


class Source(Protocol):
    name: str
    interval_s: float

    async def sample(self, now: datetime) -> tuple[dict, list[dict]]: ...


def load_sources(settings: dict) -> list[Source]:
    """Build each named source from the whole `[desk]` table."""
    found = {entry.name: entry for entry in entry_points(group=SOURCE_GROUP)}
    names = list(dict.fromkeys(settings.get("sources", ["clock"])))
    missing = [name for name in names if name not in found]
    if missing:
        available = ", ".join(sorted(found)) or "(none)"
        raise ConfigError(f"Unknown desk source {missing[0]!r}. Available: {available}")
    return [found[name].load()(settings) for name in names]
