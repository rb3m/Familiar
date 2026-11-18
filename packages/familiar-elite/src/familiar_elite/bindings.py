"""Read Elite Dangerous .binds presets and write keyboard Secondary bindings.

A preset is XML: one element per control with Primary and Secondary children
(Device and Key attributes, plus optional Modifier children). Familiar owns
only Secondary slots whose device is {NoDevice}; edits are text-level so every
other byte of the preset survives, and a JSON sidecar records the owned keys
so later edits by the game or the player are detected at session start.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from itertools import chain
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from familiar.interfaces import ConfigError, load_data

logger = logging.getLogger(__name__)

NO_DEVICE = "{NoDevice}"
KEYBOARD = "Keyboard"

PLANNED = "planned"
ALREADY_BOUND = "already-bound"
SECONDARY_OCCUPIED = "secondary-occupied"
MISSING = "missing"
NO_KEYS_LEFT = "no-keys-left"
BUILTIN_PRESET = "builtin-preset"

EMPTY_SECONDARY = re.compile(
    r'(?P<indent>^[ \t]*)<Secondary Device="\{NoDevice\}" Key=""[ \t]*/>[ \t]*(?P<ending>\r?)$',
    re.MULTILINE,
)


@dataclass(frozen=True)
class Preset:
    name: str
    path: Path
    writable: bool


@dataclass(frozen=True)
class Slot:
    """One binding slot: a device, a key on it and any modifier keys."""

    device: str
    key: str
    modifiers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Binding:
    """A control's Primary and Secondary slots; other structure stays untouched."""

    primary: Slot
    secondary: Slot


@dataclass(frozen=True)
class Assignment:
    """Plan outcome for one wanted control; key is the planned or existing key."""

    control: str
    key: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class ApplyResult:
    assigned: dict[str, str]
    backups: tuple[Path, ...]
    sidecar: Path | None


def default_backup_dir() -> Path:
    return _state_dir() / "backups"


def default_sidecar_path() -> Path:
    return _state_dir() / "owned_binds.json"


def _state_dir() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser()
    return root / "familiar" / "elite"


def _data(name: str) -> tuple[Any, str]:
    dataset = load_data("familiar_elite", name)
    return dataset.value, ", ".join(dataset.sources)


def _require(valid: bool, source: str, message: str) -> None:
    if not valid:
        raise ConfigError(f"Invalid data {source}: {message}")


def _string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def _string_mapping(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    )


def _binding_data() -> dict:
    data, source = _data("bindings")
    _require(isinstance(data, dict), source, "expected categories and controls")
    categories = data.get("categories")
    _require(_string_list(categories), source, "categories must be names")
    _require(len(set(categories)) == len(categories), source, "duplicate categories")
    controls = data.get("controls")
    _require(isinstance(controls, dict), source, "controls must be a mapping")
    valid = all(
        isinstance(name, str)
        and _string_mapping(spec)
        and set(spec) == {"category", "purpose"}
        and spec["category"] in categories
        for name, spec in controls.items()
    )
    _require(valid, source, "controls need a category and purpose")
    return data


def load_wanted_controls() -> dict[str, dict[str, str]]:
    """Wanted controls with their category and one-line purpose."""
    return _binding_data()["controls"]


def load_categories() -> list[str]:
    return _binding_data()["categories"]


def load_candidates() -> list[str]:
    """Ordered Elite key names tried for new Secondary bindings; first free key wins."""
    data, source = _data("candidate_keys")
    _require(isinstance(data, dict) and set(data) == {"candidates"}, source, "expected candidates")
    _require(_string_list(data["candidates"]), source, "candidates must be key names")
    return list(data["candidates"])


def load_key_names() -> dict[str, str]:
    """Elite key names to evdev names; unknown names are skipped when mapping."""
    data, source = _data("key_names")
    _require(isinstance(data, dict) and set(data) == {"keys"}, source, "expected keys")
    _require(_string_mapping(data["keys"]), source, "keys must map Elite names to evdev names")
    return dict(data["keys"])


def load_process_names() -> list[str]:
    """Process cmdline needles that mean Elite is running."""
    data, source = _data("processes")
    _require(isinstance(data, dict) and set(data) == {"names"}, source, "expected names")
    _require(_string_list(data["names"]), source, "names must be process names")
    return list(data["names"])


def active_presets(bindings_dir: Path, control_schemes_dir: Path | None) -> dict[str, Preset]:
    """Resolve each category, preferring versioned user presets over built-ins."""
    starts = sorted(bindings_dir.glob("StartPreset*.start"))
    if not starts:
        raise ConfigError(f"No StartPreset*.start in {bindings_dir}")
    newest = max(starts, key=lambda path: path.stat().st_mtime)
    try:
        names = newest.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"Cannot read {newest}: {exc}") from exc
    categories = load_categories()
    if len(names) == 1:
        names *= len(categories)
    if len(names) != len(categories):
        raise ConfigError(f"{newest}: expected one preset or {len(categories)} category presets")
    return {
        category: _resolve_preset(name.strip(), category, bindings_dir, control_schemes_dir)
        for category, name in zip(categories, names, strict=True)
    }


def _resolve_preset(
    name: str, category: str, bindings_dir: Path, control_schemes_dir: Path | None
) -> Preset:
    versions = _versions(bindings_dir, name)
    if versions:
        return Preset(name, max(versions.items())[1], True)
    if control_schemes_dir and (control_schemes_dir / f"{name}.binds").is_file():
        return Preset(name, control_schemes_dir / f"{name}.binds", False)
    raise ConfigError(f"Missing preset {name!r} for category {category} in {bindings_dir}")


def _preset_tables(presets: dict[str, Preset]) -> dict[Path, dict[str, Binding]]:
    return {
        path: parse_binds(_read_preset(path))
        for path in dict.fromkeys(preset.path for preset in presets.values())
    }


def _read_preset(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc


def _versions(bindings_dir: Path, name: str) -> dict[tuple[int, int], Path]:
    pattern = re.compile(rf"^{re.escape(name)}\.(\d+)\.(\d+)\.binds$")
    candidates = ((pattern.match(path.name), path) for path in bindings_dir.glob("*.*.*.binds"))
    return {(int(m[1]), int(m[2])): path for m, path in candidates if m}


def parse_binds(text: str) -> dict[str, Binding]:
    """Read Primary/Secondary slots per control; unknown structure is left alone."""
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as exc:
        raise ConfigError(f"Cannot parse binds XML: {exc}") from exc
    result = {}
    for control in root:
        primary, secondary = control.find("Primary"), control.find("Secondary")
        if primary is None and secondary is None:
            continue
        result[control.tag] = Binding(_slot(primary), _slot(secondary))
    return result


def _slot(element: ET.Element | None) -> Slot:
    if element is None:
        return Slot(NO_DEVICE, "")
    modifiers = tuple(
        (modifier.get("Device", NO_DEVICE), modifier.get("Key", ""))
        for modifier in element.findall("Modifier")
    )
    return Slot(element.get("Device", NO_DEVICE), element.get("Key", ""), modifiers)


def keyboard_keys_in_use(bindings: dict[str, Binding]) -> set[str]:
    """Every Elite Key_ name bound on the keyboard, slots and modifiers included."""
    slots = [slot for binding in bindings.values() for slot in (binding.primary, binding.secondary)]
    keys = {slot.key for slot in slots if slot.device == KEYBOARD and slot.key}
    keys.update(
        key for slot in slots for device, key in slot.modifiers if device == KEYBOARD and key
    )
    return keys


def plan_secondaries(
    presets: dict[str, Preset],
    wanted_controls: dict[str, dict[str, str]],
    candidates: list[str],
) -> list[Assignment]:
    """Choose keys free across every active file, using each control's category."""
    tables = _preset_tables(presets)
    used = set().union(*(keyboard_keys_in_use(table) for table in tables.values()))
    return [
        _plan_control(
            control, presets[spec["category"]], spec["category"], tables, candidates, used
        )
        for control, spec in wanted_controls.items()
    ]


def _plan_control(
    control: str,
    preset: Preset,
    category: str,
    tables: dict[Path, dict[str, Binding]],
    candidates: list[str],
    used: set[str],
) -> Assignment:
    binding = tables[preset.path].get(control)
    existing = _existing_keyboard_key(binding) if binding is not None else ""
    if existing:
        return Assignment(control, existing, ALREADY_BOUND)
    if not preset.writable:
        detail = f"set {category.capitalize()} controls to Custom in Options > Controls, then rerun"
        return Assignment(control, "", BUILTIN_PRESET, detail)
    if binding is None:
        return Assignment(control, "", MISSING)
    if binding.secondary.device != NO_DEVICE:
        return Assignment(control, "", SECONDARY_OCCUPIED, binding.secondary.device)
    key = next((candidate for candidate in candidates if candidate not in used), "")
    if not key:
        return Assignment(control, "", NO_KEYS_LEFT)
    used.add(key)
    return Assignment(control, key, PLANNED)


def _existing_keyboard_key(binding: Binding) -> str:
    return next(
        (
            slot.key
            for slot in (binding.primary, binding.secondary)
            if slot.device == KEYBOARD and slot.key
        ),
        "",
    )


def apply_plan(
    presets: dict[str, Preset],
    plan: list[Assignment],
    *,
    backup_dir: Path | None = None,
    sidecar_path: Path | None = None,
    proc_root: Path | None = None,
) -> ApplyResult:
    """Write planned Secondary keys, with backup, XML validation and a sidecar."""
    wanted = load_wanted_controls()
    groups: dict[Path, dict[str, str]] = {}
    for entry in plan:
        preset = presets[wanted[entry.control]["category"]]
        if entry.status != PLANNED or not preset.writable:
            continue
        groups.setdefault(preset.path, {})[entry.control] = entry.key
    if not groups:
        return ApplyResult({}, (), None)
    running = game_running(load_process_names(), proc_root)
    if running:
        raise ConfigError(f"Elite is running ({running}); close it before writing bindings")
    backup_dir = backup_dir or default_backup_dir()
    sidecar_path = sidecar_path or default_sidecar_path()
    sidecar = _read_sidecar(sidecar_path)
    prepared = [_prepare_file(path, assignments) for path, assignments in groups.items()]
    backups = []
    for path, original, data in prepared:
        backups.append(_backup(path, backup_dir, original))
        _atomic_write(path, data)
        _write_sidecar(sidecar_path, path, data, groups[path], sidecar)
    assigned = dict(chain.from_iterable(group.items() for group in groups.values()))
    return ApplyResult(assigned, tuple(backups), sidecar_path)


def _prepare_file(path: Path, assignments: dict[str, str]) -> tuple[Path, bytes, bytes]:
    try:
        original = path.read_bytes()
        text = original.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc
    return path, original, _rewrite_secondaries(text, assignments, path).encode("utf-8")


def _rewrite_secondaries(text: str, assignments: dict[str, str], path: Path) -> str:
    for control, key in assignments.items():
        text = _assign_secondary(text, control, key, path)
    _validate(text, assignments, path)
    return text


def _assign_secondary(text: str, control: str, key: str, path: Path) -> str:
    """Replace only the empty Secondary line inside one control element."""
    block = re.search(
        rf"<{re.escape(control)}(?:\s[^>]*)?>(?P<body>.*?)</{re.escape(control)}>",
        text,
        re.DOTALL,
    )
    if block is None:
        raise ConfigError(f"Cannot edit {path}: no <{control}> element")
    body, found = EMPTY_SECONDARY.subn(
        lambda match: (
            f'{match["indent"]}<Secondary Device="{KEYBOARD}" Key="{key}" />{match["ending"]}'
        ),
        block["body"],
        count=1,
    )
    if not found:
        raise ConfigError(f"Cannot edit {path}: <{control}> has no empty Secondary slot")
    return text[: block.start("body")] + body + text[block.end("body") :]


def _validate(text: str, assignments: dict[str, str], path: Path) -> None:
    parsed = parse_binds(text)
    wrong = {
        control: key
        for control, key in assignments.items()
        if control not in parsed or parsed[control].secondary != Slot(KEYBOARD, key)
    }
    if wrong:
        raise ConfigError(
            f"Refusing to write {path}: edit failed validation for {', '.join(sorted(wrong))}"
        )


def _backup(binds_path: Path, backup_dir: Path, original: bytes) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    backup = backup_dir / f"{binds_path.stem}.{stamp}.binds"
    backup.write_bytes(original)
    return backup


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        os.replace(temporary, path)
    except OSError as exc:
        raise ConfigError(f"Cannot write {path}: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_sidecar(
    sidecar_path: Path,
    binds_path: Path,
    data: bytes,
    assignments: dict[str, str],
    payload: dict,
) -> None:
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    previous = payload.get(str(binds_path), {}).get("owned", {})
    payload[str(binds_path)] = {
        "sha256": hashlib.sha256(data).hexdigest(),
        "owned": {**previous, **assignments},
    }
    _atomic_write(sidecar_path, (json.dumps(payload, indent=2) + "\n").encode("utf-8"))


def _read_sidecar(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read sidecar {path}: {exc}") from exc
    valid = isinstance(payload, dict) and all(
        isinstance(record, dict)
        and isinstance(record.get("sha256"), str)
        and _string_mapping(record.get("owned"))
        for record in payload.values()
    )
    _require(valid, str(path), "expected per-file sha256 and owned keys")
    return payload


def game_running(names: list[str], proc_root: Path | None = None) -> str | None:
    """Return the matching program name found under /proc, if any.

    Only argv[0] counts (Wine sets it to the Windows path of the .exe), so a process that
    merely mentions the name in its arguments, such as an editor or a shell, never matches.
    """
    needles = {name.lower() for name in names}
    hits = {name for name in _program_names(proc_root or Path("/proc")) if name in needles}
    return min(hits, default=None)


def _program_names(proc_root: Path) -> Iterator[str]:
    for cmdline in proc_root.glob("[0-9]*/cmdline"):
        try:
            program = cmdline.read_bytes().split(b"\x00", 1)[0]
        except OSError:
            continue
        yield program.decode("utf-8", "replace").replace("\\", "/").rsplit("/", 1)[-1].lower()


def check_sidecar(presets: dict[str, Preset], sidecar_path: Path) -> list[str]:
    """Check every recorded active preset independently."""
    try:
        sidecar = _read_sidecar(sidecar_path)
    except ConfigError as exc:
        return [f"cannot read sidecar {sidecar_path}: {exc}"]
    paths = dict.fromkeys(preset.path for preset in presets.values())
    return list(
        chain.from_iterable(
            _check_file(path, sidecar[str(path)]) for path in paths if str(path) in sidecar
        )
    )


def _check_file(binds_path: Path, sidecar: dict) -> list[str]:
    warnings = []
    try:
        data = binds_path.read_bytes()
    except OSError as exc:
        return [*warnings, f"cannot read binds file {binds_path}: {exc}"]
    if hashlib.sha256(data).hexdigest() != sidecar.get("sha256"):
        warnings.append(
            f"{binds_path.name} changed since Familiar wrote it; owned keys may be gone"
        )
    try:
        parsed = parse_binds(data.decode("utf-8-sig"))
    except (ConfigError, UnicodeError) as exc:
        return [*warnings, f"cannot parse {binds_path}: {exc}"]
    owned = sidecar.get("owned", {})
    return [
        *warnings,
        *(
            f"{control}: owned key {key} no longer bound"
            for control, key in owned.items()
            if control not in parsed or parsed[control].secondary != Slot(KEYBOARD, key)
        ),
    ]


def active_keyboard_map(
    bindings_dir: Path, control_schemes_dir: Path | None
) -> dict[str, tuple[str, ...]]:
    """Select category-specific chords; unknown controls prefer ship bindings."""
    presets = active_presets(bindings_dir, control_schemes_dir)
    tables = _preset_tables(presets)
    key_names = load_key_names()
    maps = {path: keyboard_map(table, key_names) for path, table in tables.items()}
    controls = dict.fromkeys(chain.from_iterable(tables.values()))
    wanted = load_wanted_controls()
    result = {}
    for control in controls:
        category = wanted.get(control, {}).get("category")
        chord = _active_chord(control, category, presets, maps)
        if chord:
            result[control] = chord
    return result


def _active_chord(
    control: str,
    category: str | None,
    presets: dict[str, Preset],
    maps: dict[Path, dict[str, tuple[str, ...]]],
) -> tuple[str, ...] | None:
    if category in presets:
        return maps[presets[category].path].get(control)
    ship = presets.get("ship")
    if ship and control in maps[ship.path]:
        return maps[ship.path][control]
    return next((table[control] for table in maps.values() if control in table), None)


def keyboard_map(
    bindings: dict[str, Binding], key_names: dict[str, str]
) -> dict[str, tuple[str, ...]]:
    """evdev chords (modifiers first) for every keyboard-bound control."""
    result = {}
    for control, binding in bindings.items():
        slot = _keyboard_slot(binding)
        if slot is None:
            continue
        chord = _evdev_chord(control, slot, key_names)
        if chord is not None:
            result[control] = chord
    return result


def _keyboard_slot(binding: Binding) -> Slot | None:
    return next(
        (
            slot
            for slot in (binding.primary, binding.secondary)
            if slot.device == KEYBOARD and slot.key
        ),
        None,
    )


def _evdev_chord(control: str, slot: Slot, key_names: dict[str, str]) -> tuple[str, ...] | None:
    names = [key for device, key in slot.modifiers if device == KEYBOARD]
    names.append(slot.key)
    unknown = [name for name in names if name not in key_names]
    if unknown:
        # A partial chord would press a different control, so skip the whole binding.
        logger.debug("Skipping %s: unknown Elite key name(s) %s", control, ", ".join(unknown))
        return None
    return tuple(key_names[name] for name in names)
