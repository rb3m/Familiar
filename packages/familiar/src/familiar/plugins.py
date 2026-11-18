"""Discover and instantiate versioned backend entry points."""

import sys
from importlib.metadata import entry_points

from familiar.interfaces import PLUGIN_API_VERSION, PluginError

GROUPS = {
    "keyboard": "familiar.keyboard",
    "llm": "familiar.llm",
    "stt": "familiar.stt",
    "tts": "familiar.tts",
    "games": "familiar.games",
    "services": "familiar.services",
    "commands": "familiar.commands",
}
GROUPS["ptt"] = "familiar.ptt"


def _entries(kind: str) -> list:
    if kind not in GROUPS:
        raise PluginError(f"Unknown plugin kind {kind!r}. Available kinds: {', '.join(GROUPS)}")
    return list(entry_points(group=GROUPS[kind]))


def available(kind: str) -> list[str]:
    """Return sorted registered names for a backend kind."""
    return sorted({entry.name for entry in _entries(kind)})


def load_class(kind: str, name: str) -> type:
    """Load a backend whose plugin API major version matches ours."""
    entries = _entries(kind)
    entry = next((entry for entry in entries if entry.name == name), None)
    if entry is None:
        names = ", ".join(sorted(entry.name for entry in entries)) or "(none)"
        raise PluginError(f"Unknown {kind} plugin {name!r}. Available names: {names}")
    plugin = entry.load()
    version = str(getattr(plugin, "api_version", ""))
    if version.split(".")[0] != PLUGIN_API_VERSION.split(".")[0]:
        raise PluginError(f"Plugin {name!r} has incompatible API version {version!r}")
    return plugin


def load(kind: str, name: str, settings: dict) -> object:
    """Instantiate a version-checked backend."""
    return load_class(kind, name)(settings)


def command_plugins() -> list:
    """Discover commands independently; a broken plugin cannot disable the CLI."""
    result = []
    for name in available("commands"):
        try:
            plugin = load_class("commands", name)()
            if plugin.name != name or not isinstance(plugin.help, str):
                raise PluginError("Command name/help does not match its registration")
            result.append(plugin)
        except Exception as exc:
            print(f"Warning: skipping command {name!r}: {exc}", file=sys.stderr)
    return result
