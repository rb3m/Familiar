"""The CLI must stop cleanly on SIGINT/SIGTERM even if it inherited SIGINT as ignored."""

import signal

from familiar import cli


def test_graceful_signals_restores_handlers():
    previous = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # as inherited from a background job
        cli._graceful_signals()
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
        assert signal.getsignal(signal.SIGTERM) is signal.default_int_handler
    finally:
        signal.signal(signal.SIGINT, previous[0])
        signal.signal(signal.SIGTERM, previous[1])
