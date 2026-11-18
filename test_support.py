"""Real asyncio loops with a fallback for restricted self-pipe writes."""

import asyncio


def event_loop() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()
    try:
        loop._csock.send(b"\0")
    except PermissionError:
        # Worker threads still run; polling delivers their queued completion callbacks
        # when a sandbox denies asyncio's ordinary socket wakeup.
        select = loop._selector.select
        loop._selector.select = lambda timeout=None: select(
            0.01 if timeout is None else min(timeout, 0.01)
        )
    return loop
