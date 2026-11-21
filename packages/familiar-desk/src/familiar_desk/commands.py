"""Run the independent desk feed helper."""

import argparse
import asyncio
import math
import signal
import sys

from familiar.interfaces import PLUGIN_API_VERSION, ConfigError

from familiar_desk.feed import Feed, WriterBusyError
from familiar_desk.sources import load_sources


class DeskFeedCommand:
    name = "desk-feed"
    api_version = PLUGIN_API_VERSION
    help = "Sample local desk context into a private feed"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--interval", type=float, default=None, metavar="S")

    async def run(self, args: argparse.Namespace, config) -> int:
        settings = dict(config.desk)
        if args.interval is not None:
            settings["interval_s"] = args.interval
        interval = settings.get("interval_s", 60)
        if not math.isfinite(interval) or interval <= 0:
            print("Desk feed interval must be a finite positive number", file=sys.stderr)
            return 2
        try:
            sources = load_sources(settings)
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        signals = (signal.SIGINT, signal.SIGTERM)
        for sig in signals:
            loop.add_signal_handler(sig, stop.set)
        try:
            await Feed(sources).run(stop, once=args.once)
        except WriterBusyError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        finally:
            for sig in signals:
                loop.remove_signal_handler(sig)
        return 0
