"""`familiar elite-bindings`: plan or write keyboard Secondary bindings."""

import argparse
from pathlib import Path

from familiar.interfaces import PLUGIN_API_VERSION, ConfigError

from . import bindings
from .bindings import (
    ALREADY_BOUND,
    BUILTIN_PRESET,
    MISSING,
    NO_KEYS_LEFT,
    PLANNED,
    SECONDARY_OCCUPIED,
    active_presets,
    apply_plan,
    check_sidecar,
    load_candidates,
    load_wanted_controls,
)

_TEMPLATES = {
    PLANNED: "{control}: {key} (will bind)",
    BUILTIN_PRESET: "{control}: {detail}",
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
            "--control-schemes-dir",
            type=Path,
            metavar="DIR",
            help="Built-in ControlSchemes directory (default: auto-detect)",
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
        from .paths import find_paths

        settings = config.games.get("elite", {}) if config is not None else {}
        paths = find_paths(settings)
        bindings_dir = args.bindings_dir or paths.bindings_dir
        if bindings_dir is None:
            raise ConfigError("Cannot find Elite bindings directory; set bindings_dir")
        presets = active_presets(
            bindings_dir, args.control_schemes_dir or paths.control_schemes_dir
        )
        plan = bindings.plan_secondaries(presets, load_wanted_controls(), load_candidates())
        print(f"{'Category':<12} {'Preset':<24} Writable")
        for category, preset in presets.items():
            print(f"{category:<12} {preset.name:<24} {preset.writable}")
        for assignment in plan:
            print(
                _TEMPLATES[assignment.status].format(
                    control=assignment.control, key=assignment.key, detail=assignment.detail
                )
            )
        status = _check(presets) if args.check else 0
        if not args.write:
            planned = sum(entry.status == PLANNED for entry in plan)
            print(f"Dry run: {planned} Secondary key(s) would be written; pass --write to apply")
            return status
        result = apply_plan(presets, plan)
        if not result.assigned:
            print("Nothing to write: every control is already bound or skipped.")
            return status
        print(f"Wrote {len(result.assigned)} Secondary key(s)")
        for backup in result.backups:
            print(f"Backup: {backup}")
        print(f"Sidecar: {result.sidecar}")
        print("Restart Elite to load the new keys.")
        return status


def _check(presets: dict[str, bindings.Preset]) -> int:
    warnings = check_sidecar(presets, bindings.default_sidecar_path())
    for warning in warnings:
        print(f"warning: {warning}")
    if not warnings:
        print("Sidecar check: owned keys intact.")
    return 1 if warnings else 0
