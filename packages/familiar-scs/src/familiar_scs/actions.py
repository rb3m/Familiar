"""Validated SCS action catalogue; execution lives in the adapter."""

import re
from functools import partial

from familiar.interfaces import ActionSpec

from .data_files import mapping_data, number, require, string_mapping, text
from .telemetry import Snapshot


def action_arguments(entry: dict) -> tuple[str, list[str]] | None:
    if "variants" in entry:
        variants = entry["variants"]
        return variants["argument"], list(variants["controls"])
    if "state" in entry:
        return entry["state"]["argument"], entry["state"]["choices"]
    return None


def action_mixes(catalogue: dict) -> dict:
    return {
        name: entry.get("control", entry.get("variants", {}).get("controls"))
        for name, entry in catalogue.items()
    }


def _validate_entry(name: str, entry: dict, source: str) -> None:
    allowed = {
        "control",
        "variants",
        "description",
        "state",
        "risky",
        "risky_when",
        "hold_s",
        "state_field",
        "toggle",
        "enabled",
    }
    require(isinstance(entry, dict), source, f"{name}: expected a mapping")
    require(not entry.keys() - allowed, source, f"{name}: unknown keys {entry.keys() - allowed}")
    require(text(entry.get("description")), source, f"{name}: description must be text")
    require(
        ("control" in entry) != ("variants" in entry), source, f"{name}: use control or variants"
    )
    if "control" in entry:
        require(text(entry["control"]), source, f"{name}: control must be text")
    if "variants" in entry:
        variants = entry["variants"]
        require(
            isinstance(variants, dict) and set(variants) == {"argument", "controls"},
            source,
            f"{name}: invalid variants",
        )
        require(
            text(variants["argument"])
            and string_mapping(variants["controls"])
            and bool(variants["controls"]),
            source,
            f"{name}: invalid variant controls",
        )
        require("state" not in entry, source, f"{name}: variants already define arguments")
    if "state" in entry:
        state = entry["state"]
        require(
            isinstance(state, dict) and set(state) == {"argument", "choices"},
            source,
            f"{name}: invalid state options",
        )
        require(
            text(state["argument"])
            and isinstance(state["choices"], list)
            and bool(state["choices"])
            and all(text(v) for v in state["choices"]),
            source,
            f"{name}: invalid state choices",
        )
    for flag in ("risky", "toggle", "enabled"):
        require(type(entry.get(flag, False)) is bool, source, f"{name}: {flag} must be boolean")
    require(
        number(entry.get("hold_s", 0.08)) and entry.get("hold_s", 0.08) > 0,
        source,
        f"{name}: hold_s must be positive and finite",
    )
    expected = action_arguments(entry)
    if "risky_when" in entry:
        risk = entry["risky_when"]
        require(
            isinstance(risk, dict) and bool(risk) and expected is not None,
            source,
            f"{name}: invalid risky_when",
        )
        require(
            all(k == expected[0] and v in expected[1] for k, v in risk.items()),
            source,
            f"{name}: risky_when must match an argument choice",
        )
    if "state_field" in entry:
        field = entry["state_field"]
        require(
            text(field) and type(getattr(Snapshot(), field, None)) is bool,
            source,
            f"{name}: unknown boolean state_field {field!r}",
        )
        require(
            expected is None or set(expected[1]) == {"on", "off"},
            source,
            f"{name}: state_field arguments need on/off choices",
        )


def load_catalogue(include_disabled: bool = False) -> dict:
    """Validated actions; entries with enabled: false (e.g. unreviewed drafts) are skipped."""
    catalogue, source = mapping_data("actions")
    for name, entry in catalogue.items():
        require(
            isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_]*", name) is not None,
            source,
            f"invalid action id {name!r}",
        )
        _validate_entry(name, entry, source)
    if include_disabled:
        return catalogue
    return {name: entry for name, entry in catalogue.items() if entry.get("enabled", True)}


def _matches(condition: dict, arguments: dict) -> bool:
    return all(arguments.get(key) == value for key, value in condition.items())


def action_specs(catalogue: dict | None = None) -> dict[str, ActionSpec]:
    catalogue = load_catalogue() if catalogue is None else catalogue
    result = {}
    for name, entry in catalogue.items():
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
        expected = action_arguments(entry)
        if expected:
            argument, choices = expected
            schema["properties"][argument] = {"type": "string", "enum": choices}
            schema["required"] = [argument]
        result[name] = ActionSpec(
            entry["description"],
            schema,
            risky=entry.get("risky", False),
            risky_when=partial(_matches, entry["risky_when"]) if "risky_when" in entry else None,
        )
    return result
