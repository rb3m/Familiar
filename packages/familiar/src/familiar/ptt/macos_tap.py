"""Shared macOS push-to-talk through an active session event tap."""

import asyncio
import sys
from collections.abc import AsyncIterator
from threading import Thread
from typing import Any

from familiar.interfaces import PLUGIN_API_VERSION, PluginError

_prompted = False


def native_modules(name: str) -> tuple[Any, Any]:
    """Load native APIs and check the responsible application's permission."""
    global _prompted
    if sys.platform != "darwin":
        raise PluginError(f"{name} PTT is available only on macOS; use evdev or terminal PTT")
    import ApplicationServices
    import Quartz

    if not ApplicationServices.AXIsProcessTrusted():
        if not _prompted:
            _prompted = True
            ApplicationServices.AXIsProcessTrustedWithOptions(
                {ApplicationServices.kAXTrustedCheckOptionPrompt: True}
            )
        raise PluginError(
            "Allow the responsible app (Terminal or Familiar.app) in "
            "System Settings > Privacy & Security > Accessibility, then try again"
        )
    return Quartz, ApplicationServices


class EventTapPTT:
    api_version = PLUGIN_API_VERSION
    capabilities = frozenset()

    def __init__(self, settings: dict) -> None:
        self._held = False
        self._closed = False
        self._tap = None
        self._run_loop = None
        self._thread = None
        self._queue: asyncio.Queue = asyncio.Queue()

    def _push(self, pressed: bool) -> None:
        self._held = pressed
        self._loop.call_soon_threadsafe(self._queue.put_nowait, pressed)

    def _callback(self, proxy: Any, kind: int, event: Any, context: Any) -> Any:
        q = self._quartz
        if kind in (q.kCGEventTapDisabledByTimeout, q.kCGEventTapDisabledByUserInput):
            if self._held:
                self._push(False)
            if not self._closed:
                q.CGEventTapEnable(self._tap, True)
            return event
        if kind not in self.event_types():
            return event
        pressed, consume = self.classify(kind, event)
        if pressed is not None:
            self._push(pressed)
        if consume:
            return None
        return event

    def event_types(self) -> tuple[int, ...]:
        raise NotImplementedError

    def classify(self, kind: int, event: Any) -> tuple[bool | None, bool]:
        raise NotImplementedError

    def _run(self, ready: asyncio.Future) -> None:
        q = self._quartz
        source = None
        try:
            mask = sum(1 << kind for kind in self.event_types())
            self._tap = q.CGEventTapCreate(
                q.kCGSessionEventTap,
                q.kCGHeadInsertEventTap,
                q.kCGEventTapOptionDefault,
                mask,
                self._callback,
                None,
            )
            if self._tap is None:
                raise PluginError(
                    f"Cannot create {self.name} event tap; check Accessibility permission"
                )
            self._run_loop = q.CFRunLoopGetCurrent()
            source = q.CFMachPortCreateRunLoopSource(None, self._tap, 0)
            q.CFRunLoopAddSource(self._run_loop, source, q.kCFRunLoopCommonModes)
            q.CGEventTapEnable(self._tap, True)
            self._loop.call_soon_threadsafe(self._ready, ready, None)
            q.CFRunLoopRun()
        except Exception as exc:
            self._loop.call_soon_threadsafe(self._ready, ready, exc)
        finally:
            if self._tap is not None:
                q.CFMachPortInvalidate(self._tap)
            if source is not None:
                q.CFRunLoopRemoveSource(self._run_loop, source, q.kCFRunLoopCommonModes)
            if self._held:
                self._push(False)
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)

    @staticmethod
    def _ready(future: asyncio.Future, error: Exception | None) -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
            return
        future.set_result(None)

    async def _start(self) -> None:
        self._quartz, _ = native_modules(self.name)
        self._loop = asyncio.get_running_loop()
        ready = self._loop.create_future()
        self._thread = Thread(target=self._run, args=(ready,), daemon=True)
        self._thread.start()
        # Complete startup even if the caller is cancelled, so cleanup can stop the loop.
        try:
            await asyncio.shield(ready)
        except asyncio.CancelledError:
            await ready
            raise

    async def events(self) -> AsyncIterator[bool]:
        try:
            await self._start()
            while (pressed := await self._queue.get()) is not None:
                yield pressed
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        self._closed = True
        if self._run_loop is not None:
            self._quartz.CFRunLoopStop(self._run_loop)
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join)
            self._thread = None
