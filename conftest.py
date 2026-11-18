"""Use the same sandbox-compatible loop for all workspace async tests."""

from test_support import event_loop


def pytest_asyncio_loop_factories():
    return {"default": event_loop}
