"""Conservative SCS keyboard mix parsing and player profile discovery."""

import logging
import re
import sys
from pathlib import Path
from string import ascii_lowercase

from familiar.interfaces import ConfigError

logger = logging.getLogger(__name__)
GAME_NAMES = {"ats": "American Truck Simulator", "ets2": "Euro Truck Simulator 2"}
KEY_NAMES = {name: name for name in ascii_lowercase}
KEY_NAMES.update({f"key{i}": str(i) for i in range(10)})
KEY_NAMES.update({f"num{i}": f"kp{i}" for i in range(10)})
KEY_NAMES.update({f"f{i}": f"f{i}" for i in range(1, 13)})
KEY_NAMES.update(
    dict(
        zip(
            [
                "lbracket",
                "rbracket",
                "lshift",
                "rshift",
                "lctrl",
                "lalt",
                "uarrow",
                "darrow",
                "larrow",
                "rarrow",
                "pgup",
                "pgdn",
                "numenter",
                "numplus",
                "numminus",
                "nummultiply",
                "numslash",
                "numperiod",
                "scrollock",
                "del",
                "ins",
            ],
            [
                "leftbrace",
                "rightbrace",
                "leftshift",
                "rightshift",
                "leftctrl",
                "leftalt",
                "up",
                "down",
                "left",
                "right",
                "pageup",
                "pagedown",
                "kpenter",
                "kpplus",
                "kpminus",
                "kpasterisk",
                "kpslash",
                "kpdot",
                "scrolllock",
                "delete",
                "insert",
            ],
            strict=True,
        )
    )
)
KEY_NAMES.update(
    {
        name: name
        for name in [
            "semicolon",
            "apostrophe",
            "space",
            "enter",
            "esc",
            "tab",
            "grave",
            "backspace",
            "home",
            "end",
            "pause",
            "comma",
            "period",
            "slash",
            "backslash",
            "minus",
            "equal",
        ]
    }
)
MODIFIERS = {
    "shift_only": ("leftshift",),
    "ctrl_only": ("leftctrl",),
    "alt_only": ("leftalt",),
    "no_modifier": (),
}
ACTION_MIXES = {
    "light_cycle": "light",
    "high_beams": "hblight",
    "beacon": "beacon",
    "hazards": "flasher4way",
    "flash_headlights": "lighthorn",
    "horn": "horn",
    "air_horn": "airhorn",
    "blinker": {"left": "lblinker", "right": "rblinker"},
    "wipers": "wipers",
    "engine": "engine",
    "parking_brake": "parkingbrake",
    "cruise_control": "cruiectrl",
    "retarder": {"up": "retarderup", "down": "retarderdown"},
    "lift_axle": "liftaxle",
    "trailer_attach": "attach",
    "radio": "radiotoggle",
    "radio_station": {"next": "radionext", "previous": "radioprev"},
    "quick_save": "quicksave",
    "screenshot": "screenshot",
}
# Shared ATS/ETS2 defaults. Radio power has no default (R opens the radio UI).
DEFAULT_BINDINGS = {
    mix: (key,)
    for mix, key in {
        "light": "l",
        "hblight": "k",
        "beacon": "o",
        "flasher4way": "f",
        "lighthorn": "j",
        "horn": "h",
        "airhorn": "n",
        "lblinker": "leftbrace",
        "rblinker": "rightbrace",
        "wipers": "p",
        "engine": "e",
        "parkingbrake": "space",
        "cruiectrl": "c",
        "retarderup": "semicolon",
        "retarderdown": "apostrophe",
        "liftaxle": "u",
        "attach": "t",
        "radionext": "pagedown",
        "radioprev": "pageup",
        "quicksave": "scrolllock",
        "screenshot": "f10",
    }.items()
}


def parse_controls(text: str) -> dict[str, tuple[str, ...]]:
    """Use the first keyboard alternative; never approximate unsupported expressions."""
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
            if wrapped is None or wrapped[1] not in MODIFIERS:
                continue
            modifiers = MODIFIERS[wrapped[1]]
            key = wrapped[2]
        else:
            key = match[1]
        if key not in KEY_NAMES:
            logger.debug("Skipping mix %s: unknown SCS keyboard key %s", mix, key)
            continue
        result[mix] = (*modifiers, KEY_NAMES[key])
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


def load_bindings(game: str, settings: dict) -> dict[str, tuple[str, ...]]:
    path = discover_controls(game, settings.get("profile"))
    bindings = DEFAULT_BINDINGS.copy()
    if path is not None:
        bindings = parse_controls(path.read_text(encoding="utf-8-sig"))
    for action, chord in settings.get("keys", {}).items():
        mixes = ACTION_MIXES.get(action)
        if mixes is None:
            # Direction-specific overrides: blinker_left, retarder_up, radio_station_next.
            mixes = next(
                (
                    mix
                    for name, variants in ACTION_MIXES.items()
                    if isinstance(variants, dict)
                    for variant, mix in variants.items()
                    if action == f"{name}_{variant}"
                ),
                None,
            )
        if mixes is None:
            raise ConfigError(f"Unknown SCS key override: {action}")
        if not isinstance(chord, str):
            raise ConfigError(f"Invalid neutral key chord for {action}: {chord!r}")
        keys = tuple(key.strip() for key in chord.lower().split("+"))
        if not keys or any(key not in set(KEY_NAMES.values()) for key in keys):
            raise ConfigError(f"Invalid neutral key chord for {action}: {chord!r}")
        for mix in mixes.values() if isinstance(mixes, dict) else (mixes,):
            bindings[mix] = keys
    return bindings
