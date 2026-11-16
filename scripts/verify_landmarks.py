"""Compatibility wrapper: verify_landmarks.py PACK.yaml [--max-km 5]."""

import argparse
from pathlib import Path

import httpx
from familiar_scs.curation import HEADERS, read_pack, verify_coordinates


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pack", type=Path)
    parser.add_argument("--max-km", type=float, default=5.0)
    args = parser.parse_args()
    entries = read_pack(args.pack)
    with httpx.Client(headers=HEADERS, timeout=30) as client:
        statuses = verify_coordinates(entries, client, args.max_km)
    tally: dict[str, int] = {}
    for entry in entries:
        status = statuses[entry["id"]]
        tally[status.split()[0]] = tally.get(status.split()[0], 0) + 1
        print(f"{status:14} {entry['state']} {entry['id']}")
    print("summary:", tally)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
