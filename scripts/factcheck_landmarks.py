"""Compatibility wrapper: factcheck_landmarks.py PACK.yaml REPORT.json [--delay 4]."""

import argparse
import asyncio
from pathlib import Path

import httpx
from familiar import plugins
from familiar.config import load_config
from familiar_scs.curation import HEADERS, factcheck, read_pack


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pack", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--delay", type=float, default=4.0)
    args = parser.parse_args()
    asyncio.run(_run(args))
    return 0


async def _run(args: argparse.Namespace) -> None:
    config = load_config()
    llm = plugins.load("llm", config.llm.backend, {**config.llm.settings, "temperature": 0})
    try:
        with httpx.Client(headers=HEADERS, timeout=30) as client:
            report = await factcheck(read_pack(args.pack), llm, client, args.delay, args.report)
        print(f"Report: {args.report} ({len(report)} landmarks)")
    finally:
        await llm.aclose()


if __name__ == "__main__":
    raise SystemExit(main())
