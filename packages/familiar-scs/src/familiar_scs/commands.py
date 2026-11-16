"""SCS telemetry and landmark CLI command plugins."""

import argparse
from typing import Any

from familiar.config import load_config
from familiar.fallback import close_backend
from familiar.interfaces import PLUGIN_API_VERSION, ConfigError
from familiar.plugins import load


async def _watch(args: argparse.Namespace, config=None) -> None:
    from familiar_scs.tools import watch

    config = config or load_config(args.config)
    game = args.game or config.game or "ats"
    settings = dict(config.games.get(game, {}))
    if args.replay is not None:
        settings["replay"] = args.replay
    await watch(settings, game)


async def _telemetry_record(args: argparse.Namespace, config=None) -> None:
    from familiar_scs.tools import record

    config = config or load_config(args.config)
    game = args.game or config.game or "ats"
    await record(config.games.get(game, {}), args.file)


def _landmark_arguments(landmarks: Any) -> None:
    from pathlib import Path

    actions = landmarks.add_subparsers(dest="landmark_action", required=True)
    status = actions.add_parser("status", help="Show installed and covered states (ATS only)")
    status.add_argument("--game", choices=["ats"], default="ats")
    status.add_argument("--game-dir", type=Path)
    add = actions.add_parser("add", help="Research, check and merge states or countries")
    add.add_argument("states", nargs="+", type=str.upper, metavar="XX")
    add.add_argument("--per-state", type=int, help="Override the per-region research quota")
    add.add_argument("--pack", type=Path)
    add.add_argument("--dry-run", action="store_true")
    add.add_argument("--workdir", type=Path)
    check = actions.add_parser("check", help="Check a pack without modifying it")
    check.add_argument("pack", nargs="?", type=Path)
    for command in (add, check):
        command.add_argument("--game", choices=["ats", "ets2"], default="ats")
    for command in (status, add, check):
        command.add_argument("--config")


def _landmark_status(args: argparse.Namespace, config=None) -> None:
    from pathlib import Path

    from familiar_scs.curation import owned_states, pack_states
    from familiar_scs.knowledge import KnowledgePack
    from familiar_scs.places import STATE_NAMES

    config = config or load_config(args.config)
    directory = args.game_dir or Path(
        config.games.get(args.game, {}).get(
            "game_dir", "~/.local/share/Steam/steamapps/common/American Truck Simulator"
        )
    )
    owned = owned_states(directory.expanduser())
    covered = pack_states(KnowledgePack.for_game(args.game).landmarks)
    missing = owned - covered
    for label, states in (("Owned", owned), ("Covered", covered), ("Missing", missing)):
        names = ", ".join(f"{code} ({STATE_NAMES.get(code, code)})" for code in sorted(states))
        print(f"{label}: {names or '-'}")
    if missing:
        print(f"Next: familiar landmarks add {' '.join(sorted(missing))}")


def _landmark_summary(original: list[dict], kept: list[dict]) -> None:
    from collections import Counter

    from familiar_scs.curation import pack_states

    originals = {entry["id"]: entry for entry in original}
    totals = Counter(entry["state"] for entry in original)
    counts: Counter[str] = Counter()
    corrected: Counter[str] = Counter()
    removed: Counter[str] = Counter()
    for entry in kept:
        state = entry["state"]
        before = set(originals[entry["id"]]["facts"])
        after = set(entry["facts"])
        counts[state] += 1
        corrected[state] += len(after - before)
        removed[state] += len(before - after)
    for state in sorted(pack_states(original)):
        print(
            f"{state}: kept {counts[state]}, dropped {totals[state] - counts[state]}, "
            f"facts corrected {corrected[state]}, facts removed {removed[state]}"
        )


async def _landmarks(args: argparse.Namespace, config=None) -> None:
    import httpx

    if args.landmark_action == "status":
        _landmark_status(args, config)
        return
    try:
        await _curate_landmarks(args, config)
    except (OSError, ValueError, httpx.HTTPError, TimeoutError) as exc:
        raise ConfigError(f"Landmark curation failed: {exc}") from exc


async def _curate_landmarks(args: argparse.Namespace, config=None) -> None:
    from datetime import UTC, datetime
    from pathlib import Path

    import httpx

    from familiar_scs import curation

    adding = args.landmark_action == "add"
    pack = args.pack or curation.pack_path(args.game)
    workdir = Path("data/landmarks") / datetime.now(UTC).isoformat().replace("+00:00", "Z")
    if adding:
        workdir = args.workdir or workdir
        entries = await curation.research(args.states, args.per_state, workdir, game=args.game)
    if not adding:
        entries = curation.read_pack(pack, game=args.game)
    workdir.mkdir(parents=True, exist_ok=True)
    report_path = workdir / "factcheck.json"
    with httpx.Client(headers=curation.HEADERS, timeout=30) as client:
        statuses = curation.verify_coordinates(entries, client)
        rejected = [entry for entry in entries if statuses[entry["id"]] != "OK"]
        for entry in rejected:
            print(f"{entry['id']}: {statuses[entry['id']]}")
        print(f"Coordinates: {len(entries) - len(rejected)}/{len(entries)} OK")
        candidates = [entry for entry in entries if statuses[entry["id"]] == "OK"]
        if not adding:
            candidates = entries
        config = config or load_config(args.config)
        llm = load("llm", config.llm.backend, {**config.llm.settings, "temperature": 0})
        try:
            report = await curation.factcheck(candidates, llm, client, 4.0, report_path)
        finally:
            await close_backend(llm)
    kept, left_out = curation.apply_report(candidates, report)
    spacing = curation.GAME_MIN_SPACING_KM[args.game]
    existing = []
    if adding and spacing is not None and pack.exists():
        existing = curation.read_pack(pack, game=args.game)
    if adding and spacing is not None:
        kept, spaced_out = curation.filter_spacing(kept, existing, spacing)
        print(f"Spacing dropped: {', '.join(spaced_out) or '-'}")
    _landmark_summary(entries, kept)
    print(f"Fact-check left out: {', '.join(left_out) or '-'}")
    print(f"Report: {report_path}")
    if not adding:
        return
    staged = workdir / "landmarks.checked.yaml"
    curation.write_pack(staged, kept)
    print(f"Staged: {staged}")
    if args.dry_run:
        return
    added, skipped = curation.merge(pack, kept, game=args.game)
    print(f"Merged into {pack}: added {added}, skipped {skipped}")


class Watch:
    name = "watch"
    api_version = PLUGIN_API_VERSION
    help = "Watch live game telemetry"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--game", required=True, metavar="NAME")
        parser.add_argument("--replay", metavar="FILE")

    async def run(self, args, config) -> int:
        await _watch(args, config)
        return 0


class TelemetryRecord:
    name = "telemetry-record"
    api_version = PLUGIN_API_VERSION
    help = "Record game telemetry"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("file", metavar="FILE")
        parser.add_argument("--game", metavar="NAME")

    async def run(self, args, config) -> int:
        await _telemetry_record(args, config)
        return 0


class Landmarks:
    name = "landmarks"
    api_version = PLUGIN_API_VERSION
    help = "Curate landmark knowledge packs"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        _landmark_arguments(parser)

    async def run(self, args, config) -> int:
        await _landmarks(args, config)
        return 0
