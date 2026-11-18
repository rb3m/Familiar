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
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
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

EMPTY_SECONDARY = re.compile(
    r'(?P<indent>^[ \t]*)<Secondary Device="\{NoDevice\}" Key=""[ \t]*/>[ \t]*(?P<ending>\r?)$',
    re.MULTILINE,
)


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
    backup: Path | None
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


def load_wanted_controls() -> dict[str, str]:
    """Controls Familiar wants on the keyboard, mapped to a one-line purpose."""
    data, source = _data("bindings")
    _require(isinstance(data, dict) and set(data) == {"controls"}, source, "expected controls")
    _require(_string_mapping(data["controls"]), source, "controls must map names to purposes")
    return dict(data["controls"])


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


def active_preset(bindings_dir: Path) -> Path:
    """Resolve the preset Elite loads from the newest StartPreset*.start file."""
    starts = sorted(bindings_dir.glob("StartPreset*.start"))
    if not starts:
        raise ConfigError(f"No StartPreset*.start in {bindings_dir}")
    newest = max(starts, key=lambda path: path.stat().st_mtime)
    try:
        lines = newest.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"Cannot read {newest}: {exc}") from exc
    names = [line.strip() for line in lines if line.strip() and line.strip().lower() != "default"]
    if not names:
        raise ConfigError(f"{newest} lists no custom preset")
    tally = Counter(names)
    common = tally.most_common()
    if len(common) > 1 and common[0][1] == common[1][1]:
        raise ConfigError(f"Ambiguous presets in {newest}: {', '.join(sorted(tally))}")
    unresolved = [name for name in tally if not _versions(bindings_dir, name)]
    if unresolved:
        raise ConfigError(
            f"Preset(s) without a .binds file in {bindings_dir}: {', '.join(sorted(unresolved))}"
        )
    name = common[0][0]
    return max(_versions(bindings_dir, name).items())[1]


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
    bindings: dict[str, Binding], wanted_controls: Iterable[str], candidates: list[str]
) -> list[Assignment]:
    """Pick a free candidate key for each wanted control with an empty Secondary."""
    used = keyboard_keys_in_use(bindings)
    plan = []
    for control in wanted_controls:
        binding = bindings.get(control)
        if binding is None:
            plan.append(Assignment(control, "", MISSING))
            continue
        existing = _existing_keyboard_key(binding)
        if existing:
            plan.append(Assignment(control, existing, ALREADY_BOUND))
            continue
        if binding.secondary.device != NO_DEVICE:
            plan.append(Assignment(control, "", SECONDARY_OCCUPIED, binding.secondary.device))
            continue
        key = next((candidate for candidate in candidates if candidate not in used), "")
        if not key:
            plan.append(Assignment(control, "", NO_KEYS_LEFT))
            continue
        used.add(key)
        plan.append(Assignment(control, key, PLANNED))
    return plan


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
    binds_path: Path,
    plan: list[Assignment],
    *,
    backup_dir: Path | None = None,
    sidecar_path: Path | None = None,
    proc_root: Path | None = None,
) -> ApplyResult:
    """Write planned Secondary keys, with backup, XML validation and a sidecar."""
    assignments = {entry.control: entry.key for entry in plan if entry.status == PLANNED}
    if not assignments:
        return ApplyResult({}, None, None)
    running = game_running(load_process_names(), proc_root)
    if running:
        raise ConfigError(f"Elite is running ({running}); close it before writing bindings")
    backup_dir = backup_dir or default_backup_dir()
    sidecar_path = sidecar_path or default_sidecar_path()
    try:
        original = binds_path.read_bytes()
        text = original.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"Cannot read {binds_path}: {exc}") from exc
    data = _rewrite_secondaries(text, assignments, binds_path).encode("utf-8")
    backup = _backup(binds_path, backup_dir, original)
    _atomic_write(binds_path, data)
    _write_sidecar(sidecar_path, binds_path, data, assignments)
    return ApplyResult(assignments, backup, sidecar_path)


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
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
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
    sidecar_path: Path, binds_path: Path, data: bytes, assignments: dict[str, str]
) -> None:
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "preset": str(binds_path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "owned": dict(assignments),
    }
    sidecar_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def game_running(names: list[str], proc_root: Path | None = None) -> str | None:
    """Return the matching process name found under /proc, if any."""
    needles = [name.lower() for name in names]
    hits = {
        needle
        for text in _process_texts(proc_root or Path("/proc"))
        for needle in needles
        if needle in text
    }
    return min(hits, default=None)


def _process_texts(proc_root: Path) -> Iterator[str]:
    for cmdline in proc_root.glob("[0-9]*/cmdline"):
        try:
            yield cmdline.read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace").lower()
        except OSError:
            continue


def check_sidecar(binds_path: Path, sidecar_path: Path) -> list[str]:
    """Warnings when the preset drifted from what Familiar wrote; [] when clean."""
    try:
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        return [f"cannot read sidecar {sidecar_path}: {exc}"]
    warnings = []
    recorded = sidecar.get("preset")
    if recorded and Path(recorded) != binds_path:
        warnings.append(f"sidecar records {recorded}, not {binds_path}")
    try:
        data = binds_path.read_bytes()
    except OSError as exc:
        return [*warnings, f"cannot read binds file {binds_path}: {exc}"]
    if hashlib.sha256(data).hexdigest() != sidecar.get("sha256"):
        warnings.append(
            f"{binds_path.name} changed since Familiar wrote it; owned keys may be gone"
        )
    parsed = parse_binds(data.decode("utf-8"))
    owned = sidecar.get("owned", {})
    return [
        *warnings,
        *(
            f"{control}: owned key {key} no longer bound"
            for control, key in owned.items()
            if control not in parsed or parsed[control].secondary != Slot(KEYBOARD, key)
        ),
    ]


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
    chord = []
    for name in names:
        evdev = key_names.get(name)
        if evdev is None:
            logger.debug("Skipping unknown Elite key name %s on %s", name, control)
            continue
        chord.append(evdev)
    if slot.key not in key_names:
        return None
    return tuple(chord)
