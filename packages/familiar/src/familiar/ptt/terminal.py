"""Enter toggles recording; a daemon reader avoids blocking interpreter shutdown."""

import asyncio
import sys
from collections.abc import AsyncIterator
from threading import Thread

from familiar.interfaces import PLUGIN_API_VERSION


class TerminalPTT:
    name = "terminal"
    api_version = PLUGIN_API_VERSION
    capabilities = frozenset()

    def __init__(self, settings: dict) -> None:
        self._closed = False
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    def _read(self, loop: asyncio.AbstractEventLoop) -> None:
        while not self._closed:
            line = sys.stdin.readline()
            if self._closed or loop.is_closed():
                return
            try:
                loop.call_soon_threadsafe(self._queue.put_nowait, line)
            except RuntimeError:
                return
            if not line:
                return

    async def events(self) -> AsyncIterator[bool]:
        print("Press Enter to start talking; press Enter again to stop. Ctrl-C exits.")
        Thread(target=self._read, args=(asyncio.get_running_loop(),), daemon=True).start()
        pressed = False
        while not self._closed:
            line = await self._queue.get()
            if not line:
                return
            pressed = not pressed
            yield pressed

    async def aclose(self) -> None:
        self._closed = True
        self._queue.put_nowait("")
