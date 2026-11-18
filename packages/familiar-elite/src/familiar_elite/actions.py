"""Elite action catalogue and refreshable active keyboard bindings."""

import logging
import math

from familiar.interfaces import ActionSpec, ConfigError, load_data

from . import bindings

logger = logging.getLogger(__name__)


def _check_sidecar(presets):
    for warning in bindings.check_sidecar(presets, bindings.default_sidecar_path()):
        logger.warning("Elite bindings: %s", warning)


def _require(valid, message):
    if not valid:
        raise ConfigError(f"Elite actions: {message}")


def _number(value, minimum=0):
    return type(value) in (int, float) and math.isfinite(value) and value >= minimum


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _validate_step(step):
    _require(isinstance(step, dict), "step must be a mapping")
    _require(not step.keys() - {"control", "wait_s"}, "unknown step keys")
    _require(_text(step.get("control")), "step needs a control")
    _require(_number(step.get("wait_s", 0)), "wait_s must be nonnegative and finite")


def _validate_entry(name, entry, flags):
    _require(isinstance(entry, dict), f"{name} must be a mapping")
    allowed = {
        "description",
        "control",
        "on_foot_control",
        "flag",
        "states",
        "steps",
        "pips",
        "hold_s",
    }
    _require(not entry.keys() - allowed, f"{name}: unknown keys")
    _require(_text(entry.get("description")), f"{name}: description required")
    _require(
        _number(entry.get("hold_s", 0.08)) and entry.get("hold_s", 0.08) > 0,
        f"{name}: hold_s must be positive and finite",
    )
    if "control" in entry:
        _require(_text(entry["control"]), f"{name}: invalid control")
        _require("steps" not in entry and "pips" not in entry, f"{name}: mixed action types")
        _require(entry.get("flag") in flags, f"{name}: unknown status flag")
        states = entry.get("states")
        _require(isinstance(states, dict) and bool(states), f"{name}: states required")
        _require(
            all(_text(k) and type(v) is bool for k, v in states.items()),
            f"{name}: states must map names to booleans",
        )
        _require(
            _text(entry.get("on_foot_control", entry["control"])),
            f"{name}: invalid on-foot control",
        )
        return
    _require(
        not {"flag", "states", "on_foot_control"} & entry.keys(),
        f"{name}: toggle fields require a control",
    )
    steps = entry.get("steps")
    _require(isinstance(steps, list) and bool(steps), f"{name}: steps required")
    for step in steps:
        _validate_step(step)
    if name == "request_docking":
        _require("pips" not in entry, "request_docking cannot set pips")
        return
    pips = entry.get("pips")
    _require(isinstance(pips, list) and len(pips) == 3, f"{name}: three pips required")
    _require(
        all(type(v) is int and 0 <= v <= 8 for v in pips) and sum(pips) == 12,
        f"{name}: invalid half-pips",
    )


def load_catalogue():
    catalogue = load_data("familiar_elite", "actions").value
    bits = load_data("familiar_elite", "status_flags").value
    flags = set().union(*(set(names) for names in bits.values()))
    _require(isinstance(catalogue, dict), "catalogue must be a mapping")
    for name, entry in catalogue.items():
        _require(_text(name), "action names must be text")
        _validate_entry(name, entry, flags)
    return catalogue


def hold_times(catalogue, overrides):
    _require(isinstance(overrides, dict), "hold_s must be a mapping")
    result = {name: entry.get("hold_s", 0.08) for name, entry in catalogue.items()}
    for name, value in overrides.items():
        _require(name in catalogue, f"unknown hold_s action: {name}")
        _require(_number(value) and value > 0, f"hold_s.{name} must be positive and finite")
        result[name] = value
    return result


def action_specs(catalogue):
    return {name: _spec(entry) for name, entry in catalogue.items()}


def _spec(entry):
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    if "states" in entry:
        schema["properties"]["state"] = {"type": "string", "enum": list(entry["states"])}
        schema["required"] = ["state"]
    return ActionSpec(entry["description"], schema)


class BindingSource:
    """Re-resolve category presets so preset switches and file edits both refresh keys."""

    def __init__(self, paths):
        self.signature = None
        self.table = {}
        self.error = None
        self.refresh(paths, check_sidecar=True)

    def refresh(self, paths, *, check_sidecar=False):
        try:
            return self._refresh(paths, check_sidecar)
        except (ConfigError, OSError) as exc:
            self.signature, self.table, self.error = None, {}, str(exc)
            logger.warning("Elite bindings: %s", exc)
            return self.table

    def _refresh(self, paths, check_sidecar):
        if paths.bindings_dir is None:
            self.signature, self.table = None, {}
            self.error = "Elite bindings directory not found; configure games.elite.bindings_dir"
            return self.table
        presets = bindings.active_presets(paths.bindings_dir, paths.control_schemes_dir)
        signature = tuple(
            (category, preset.path, preset.path.stat().st_mtime_ns)
            for category, preset in presets.items()
        )
        if check_sidecar:
            _check_sidecar(presets)
        if signature != self.signature:
            self.table = bindings.active_keyboard_map(paths.bindings_dir, paths.control_schemes_dir)
            self.signature = signature
        self.error = None
        return self.table
