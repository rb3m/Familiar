"""Merge independently held push-to-talk controls into one recording state."""

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import aclosing

from familiar.interfaces import PLUGIN_API_VERSION, PluginError, PushToTalk


class CombinedPTT:
    name = "combined"
    api_version = PLUGIN_API_VERSION
    capabilities = frozenset()

    def __init__(self, sources: list[tuple[str, PushToTalk]]) -> None:
        self.sources = sources
        self._tasks: list[asyncio.Task] = []

    async def _feed(self, label: str, source: PushToTalk, queue: asyncio.Queue) -> None:
        reason = "end of events"
        try:
            async with aclosing(source.events()) as events:
                async for pressed in events:
                    await queue.put((label, pressed))
        except PluginError as exc:
            reason = str(exc)
        except asyncio.CancelledError:
            raise
        finally:
            queue.put_nowait((label, None))
        print(f"PTT {label} stopped: {reason}", file=sys.stderr, flush=True)

    async def events(self) -> AsyncIterator[bool]:
        queue: asyncio.Queue = asyncio.Queue()
        held: set[str] = set()
        remaining = len(self.sources)
        self._tasks = [
            asyncio.create_task(self._feed(label, source, queue)) for label, source in self.sources
        ]
        try:
            while remaining:
                label, pressed = await queue.get()
                previous = bool(held)
                held.discard(label)
                if pressed:
                    held.add(label)
                if pressed is None:
                    remaining -= 1
                if bool(held) != previous:
                    yield bool(held)
        finally:
            await self._cancel_workers()

    async def _cancel_workers(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    async def aclose(self) -> None:
        await self._cancel_workers()
        await asyncio.gather(*(source.aclose() for _, source in self.sources))
