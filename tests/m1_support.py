"""Run thread-based tests even in sandboxes that deny local socket writes."""

import asyncio
from collections.abc import Coroutine
from typing import Any


def _loop() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()
    try:
        loop._csock.send(b"\0")
    except PermissionError:
        # Some restricted runners deny asyncio's self-pipe send(). A short poll
        # lets the real worker threads complete without mocking their work.
        select = loop._selector.select
        loop._selector.select = lambda timeout=None: select(
            0.01 if timeout is None else min(timeout, 0.01)
        )
    return loop


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    with asyncio.Runner(loop_factory=_loop) as runner:
        return runner.run(coro)
