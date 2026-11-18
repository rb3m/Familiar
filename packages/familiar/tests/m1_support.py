"""Run thread-based tests even in sandboxes that deny local socket writes."""

import asyncio
from collections.abc import Coroutine
from typing import Any

from test_support import event_loop


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    with asyncio.Runner(loop_factory=event_loop) as runner:
        return runner.run(coro)
