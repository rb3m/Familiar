"""One-shot probe and JSONL flight recording commands."""

import json
import sys
import time
from pathlib import Path

from familiar.interfaces import PLUGIN_API_VERSION

from .adapter import KspAdapter
from .telemetry import KrpcTelemetry


class KspProbe:
    name = "ksp-probe"
    api_version = PLUGIN_API_VERSION
    help = "Inspect a single KSP kRPC snapshot"

    def add_arguments(self, parser) -> None:
        pass

    async def run(self, args, config) -> int:
        adapter = KspAdapter(dict(config.games.get("ksp", {})))
        source = KrpcTelemetry(dict(config.games.get("ksp", {})))
        try:
            adapter.latest = await source.poll()
            if not adapter.latest.connected:
                print(f"KSP kRPC server unreachable: {source.last_error}", file=sys.stderr)
                return 1
            print(adapter.describe())
            print(json.dumps(adapter.latest.to_dict(), sort_keys=True))
            return 0
        finally:
            await source.aclose()
            await adapter.aclose()


class KspRecord:
    name = "ksp-record"
    api_version = PLUGIN_API_VERSION
    help = "Record KSP snapshots to JSONL until interrupted"

    def add_arguments(self, parser) -> None:
        parser.add_argument("file", type=Path)

    async def run(self, args, config) -> int:
        source = KrpcTelemetry(dict(config.games.get("ksp", {})))
        try:
            with args.file.open("w", encoding="utf-8") as stream:
                async for snapshot in source:
                    stream.write(
                        json.dumps(dict(ts=time.monotonic(), snapshot=snapshot.to_dict())) + "\n"
                    )
                    stream.flush()
        finally:
            await source.aclose()
        return 0
