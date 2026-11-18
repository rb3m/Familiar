"""`familiar elite-bindings`: plan or write keyboard Secondary bindings."""

import argparse
from pathlib import Path

from familiar.interfaces import PLUGIN_API_VERSION

from . import bindings
from .bindings import (
    ALREADY_BOUND,
    MISSING,
    NO_KEYS_LEFT,
    PLANNED,
    SECONDARY_OCCUPIED,
    active_preset,
    apply_plan,
    check_sidecar,
    load_candidates,
    load_wanted_controls,
    parse_binds,
)

_TEMPLATES = {
    PLANNED: "{control}: {key} (will bind)",
    ALREADY_BOUND: "{control}: {key} (already bound)",
    SECONDARY_OCCUPIED: "{control}: secondary occupied by {detail}; skipped",
    MISSING: "{control}: no such control in this preset; skipped",
    NO_KEYS_LEFT: "{control}: no free candidate key left; skipped",
}


class EliteBindings:
    name = "elite-bindings"
    api_version = PLUGIN_API_VERSION
    help = "Plan or write Elite keyboard Secondary bindings"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--bindings-dir",
            type=Path,
            metavar="DIR",
            help="Elite Options/Bindings directory (default: auto-detect)",
        )
        parser.add_argument(
            "--write", action="store_true", help="Apply the plan (dry run without it)"
        )
        parser.add_argument(
            "--check",
            action="store_true",
            help="Warn when the preset changed since Familiar wrote it",
        )

    def run(self, args: argparse.Namespace, config) -> int:
        bindings_dir = args.bindings_dir or _detected_dir(config)
        preset = active_preset(bindings_dir)
        table = parse_binds(preset.read_text(encoding="utf-8"))
        plan = bindings.plan_secondaries(table, load_wanted_controls(), load_candidates())
        print(f"Preset: {preset}")
        for assignment in plan:
            print(
                _TEMPLATES[assignment.status].format(
                    control=assignment.control, key=assignment.key, detail=assignment.detail
                )
            )
        status = _check(preset) if args.check else 0
        if not args.write:
            planned = sum(entry.status == PLANNED for entry in plan)
            print(f"Dry run: {planned} Secondary key(s) would be written; pass --write to apply")
            return status
        result = apply_plan(preset, plan)
        if not result.assigned:
            print("Nothing to write: every control is already bound or skipped.")
            return status
        print(f"Wrote {len(result.assigned)} Secondary key(s); backup: {result.backup}")
        print(f"Sidecar: {result.sidecar}")
        print("Restart Elite to load the new keys.")
        return status


def _detected_dir(config) -> Path:
    from .paths import find_paths

    return find_paths(config.games.get("elite", {})).bindings_dir


def _check(preset: Path) -> int:
    warnings = check_sidecar(preset, bindings.default_sidecar_path())
    for warning in warnings:
        print(f"warning: {warning}")
    if not warnings:
        print("Sidecar check: owned keys intact.")
    return 1 if warnings else 0
