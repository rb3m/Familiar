"""Small async helpers for coordinator CLI wiring."""

import asyncio
from collections.abc import Callable
from pathlib import Path

from familiar.interfaces import ConfigError

from .adapter import AtsAdapter, Ets2Adapter
from .telemetry import DEFAULT_URL
from .telemetry import record as record_telemetry


async def watch(settings: dict, game: str, print_fn: Callable[[str], object] = print) -> None:
    adapters = {"ats": AtsAdapter, "ets2": Ets2Adapter}
    if game not in adapters:
        raise ConfigError(f"Unknown SCS game: {game}")
    adapter = adapters[game](settings)

    async def consume() -> None:
        async for event in adapter.events():
            print_fn(f"{event.priority} {event.kind}: {event.data['summary']}")

    task = asyncio.create_task(consume())
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=10)
            description = adapter.describe()
            if description:
                print_fn(description)
            if done:
                await task
                return
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await adapter.aclose()


async def record(settings: dict, path: str | Path) -> None:
    await record_telemetry(
        settings.get("url", DEFAULT_URL), path, settings.get("poll_interval_s", 1.0)
    )
