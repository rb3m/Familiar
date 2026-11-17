"""Conservative SCS keyboard mix parsing and player profile discovery."""

import logging
import re
import sys
from pathlib import Path

from familiar.interfaces import ConfigError

from .actions import action_mixes, load_catalogue
from .data_files import mapping_data, require, string_mapping
from .data_files import text as is_text

logger = logging.getLogger(__name__)
GAME_NAMES = {"ats": "American Truck Simulator", "ets2": "Euro Truck Simulator 2"}


def load_key_names() -> tuple[dict, dict]:
    data, source = mapping_data("key_names")
    require(set(data) == {"keys", "modifiers"}, source, "expected keys and modifiers")
    require(string_mapping(data["keys"]), source, "keys must map SCS names to neutral names")
    modifiers = data["modifiers"]
    require(isinstance(modifiers, dict), source, "modifiers must be a mapping")
    neutral = set(data["keys"].values())
    require(
        all(
            is_text(k) and isinstance(v, list) and all(is_text(key) and key in neutral for key in v)
            for k, v in modifiers.items()
        ),
        source,
        "invalid modifier chords",
    )
    return data["keys"], {k: tuple(v) for k, v in modifiers.items()}


def key_chord(value: object, neutral: set[str], source: str) -> tuple[str, ...]:
    require(is_text(value), source, f"invalid neutral key chord {value!r}")
    keys = tuple(key.strip() for key in value.lower().split("+"))
    require(all(key in neutral for key in keys), source, f"invalid neutral key chord {value!r}")
    return keys


def default_bindings() -> dict[str, tuple[str, ...]]:
    data, source = mapping_data("default_keys")
    neutral = set(load_key_names()[0].values())
    require(all(is_text(k) for k in data), source, "control names must be text")
    return {mix: key_chord(chord, neutral, source) for mix, chord in data.items()}


# Compatibility views; runtime consumers load fresh data when recreated.
DEFAULT_BINDINGS = default_bindings()


def parse_controls(text: str) -> dict[str, tuple[str, ...]]:
    """Use the first keyboard alternative; never approximate unsupported expressions."""
    key_names, modifier_names = load_key_names()
    result = {}
    for mix, expression in re.findall(r"\bmix\s+(\w+)\s+`([^`]*)`", text):
        alternative = next(
            (part.strip() for part in expression.split("|") if "keyboard." in part), None
        )
        if alternative is None:
            continue
        match = re.fullmatch(r"keyboard\.(\w+)\?0", alternative)
        modifiers = ()
        if match is None:
            wrapped = re.fullmatch(r"modifier\((\w+),\s*keyboard\.(\w+)\?0\)", alternative)
            if wrapped is None or wrapped[1] not in modifier_names:
                continue
            modifiers = modifier_names[wrapped[1]]
            key = wrapped[2]
        else:
            key = match[1]
        if key not in key_names:
            logger.debug("Skipping mix %s: unknown SCS keyboard key %s", mix, key)
            continue
        result[mix] = (*modifiers, key_names[key])
    return result


def discover_controls(game: str, profile: str | None = None) -> Path | None:
    """Find an explicit profile, or the newest controls file across both profile stores."""
    filename = "controls.sii" if sys.platform == "darwin" else "controls_linux.sii"
    base = (
        Path.home()
        / ("Library/Application Support" if sys.platform == "darwin" else ".local/share")
        / GAME_NAMES[game]
    )
    if profile:
        directory = Path(profile).expanduser()
        if directory.is_file():
            return directory
        if directory.is_absolute() or len(directory.parts) > 1 or directory.is_dir():
            candidate = directory / filename
            return candidate if candidate.is_file() else None
    candidates = [
        path
        for store in ("steam_profiles", "profiles")
        for path in (base / store).glob(f"{profile or '*'}/{filename}")
        if path.is_file()
    ]
    return max(candidates, key=lambda path: path.stat().st_mtime, default=None)


def read_controls(path: Path) -> dict[str, tuple[str, ...]]:
    """Reject incomplete writes before replacing a working binding table."""
    try:
        contents = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"Cannot read controls {path}: {exc}") from exc
    definitions = re.findall(r"\bmix\s+(\w+)\s+`([^`]*)`", contents)
    require(bool(definitions), str(path), "no complete mix definitions")
    require(
        len(definitions) == len(re.findall(r"\bmix\s+", contents)),
        str(path),
        "incomplete mix definition",
    )
    require(contents.count("{") == contents.count("}"), str(path), "incomplete controls document")
    require(
        len({name for name, _ in definitions}) == len(definitions),
        str(path),
        "duplicate mix definitions",
    )
    return parse_controls(contents)


def bindings_for_path(path: Path | None, settings: dict, mixes: dict) -> dict[str, tuple[str, ...]]:
    bindings = default_bindings() if path is None else read_controls(path)
    neutral = set(load_key_names()[0].values())
    overrides = dict(mixes)
    overrides.update(
        {
            f"{name}_{variant}": mix
            for name, variants in mixes.items()
            if isinstance(variants, dict)
            for variant, mix in variants.items()
        }
    )
    for action, chord in settings.get("keys", {}).items():
        controls = overrides.get(action)
        if controls is None:
            raise ConfigError(f"Unknown SCS key override: {action}")
        keys = key_chord(chord, neutral, f"settings keys.{action}")
        for mix in controls.values() if isinstance(controls, dict) else (controls,):
            bindings[mix] = keys
    return bindings


def load_bindings(game: str, settings: dict) -> dict[str, tuple[str, ...]]:
    return bindings_for_path(
        discover_controls(game, settings.get("profile")), settings, action_mixes(load_catalogue())
    )


class BindingSource:
    """Discover and atomically refresh profile bindings, retaining the last good table."""

    def __init__(self, game: str, settings: dict, mixes: dict) -> None:
        self.game = game
        self.settings = settings
        self.mixes = mixes
        self.path, self._stamp = self._discover()
        self.table = bindings_for_path(self.path, settings, mixes)

    def _discover(self) -> tuple[Path | None, tuple]:
        path = discover_controls(self.game, self.settings.get("profile"))
        if path is None:
            return None, (None,)
        stat = path.stat()
        return path, (path, stat.st_mtime_ns, stat.st_size)

    def refresh(self) -> dict[str, tuple[str, ...]]:
        try:
            path, stamp = self._discover()
            if stamp == self._stamp:
                return self.table
            table = bindings_for_path(path, self.settings, self.mixes)
            # A write during parsing is not a stable candidate; retry next press.
            if self._discover()[1] != stamp:
                raise ConfigError(f"Controls changed while reading {path}")
            self.path, self._stamp, self.table = path, stamp, table
        except (ConfigError, OSError, UnicodeError) as exc:
            logger.warning("Cannot reload SCS bindings; keeping previous table: %s", exc)
        return self.table
