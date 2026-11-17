"""Load application settings, resolving paths relative to their TOML file."""

import math
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from familiar.flavor import (
    FLAVOR_COOLDOWN_MIN,
    FLAVOR_ITEM_COOLDOWN_DAYS,
    FLAVOR_PROBABILITY,
)
from familiar.interfaces import ConfigError

MEMORY_DEFAULTS = {
    "enabled": True,
    "path": "",
    "recall_probability": 0.25,
    "recall_cooldown_min": 45,
}

FLAVOR_DEFAULTS = {
    "enabled": True,
    "probability": FLAVOR_PROBABILITY,
    "cooldown_min": FLAVOR_COOLDOWN_MIN,
    "item_cooldown_days": FLAVOR_ITEM_COOLDOWN_DAYS,
}


@dataclass
class BackendConfig:
    backend: str
    settings: dict = field(default_factory=dict)
    fallback: "BackendConfig | None" = None


@dataclass
class AppConfig:
    language: str = "en"
    persona: str = "mira"
    personas_dir: Path | None = None
    user_personas_dir: Path = field(
        default_factory=lambda: Path("~/.config/familiar/personas").expanduser()
    )
    transcript_dir: Path = Path("data/transcripts")
    llm: BackendConfig = field(default_factory=lambda: BackendConfig("gemini"))
    ptt: BackendConfig = field(default_factory=lambda: BackendConfig("terminal"))
    stt: BackendConfig | None = None
    tts: BackendConfig | None = None
    game: str | None = None
    games: dict[str, dict] = field(default_factory=dict)
    director: dict = field(default_factory=dict)
    memory: dict = field(default_factory=lambda: MEMORY_DEFAULTS.copy())
    flavor: dict = field(default_factory=lambda: FLAVOR_DEFAULTS.copy())


def _backend(data: dict, kind: str, default: str | None = None) -> BackendConfig | None:
    section = data.get(kind, {})
    if not isinstance(section, dict):
        raise ConfigError(f"[{kind}] must be a table")
    name = section.get("backend", default)
    if name is None:
        return None
    if not isinstance(name, str) or not name:
        raise ConfigError(f"{kind}.backend must be a non-empty string")
    settings = section.get(name, {})
    if not isinstance(settings, dict):
        raise ConfigError(f"[{kind}.{name}] must be a table")
    fallback = section.get("fallback")
    if fallback is None:
        return BackendConfig(name, settings.copy())
    if not isinstance(fallback, str) or not fallback or fallback == name:
        raise ConfigError(f"{kind}.fallback must name a different backend")
    fallback_settings = section.get(fallback, {})
    if not isinstance(fallback_settings, dict):
        raise ConfigError(f"[{kind}.{fallback}] must be a table")
    return BackendConfig(name, settings.copy(), BackendConfig(fallback, fallback_settings.copy()))


def _path(value: object, base: Path) -> Path:
    if not isinstance(value, str):
        raise ConfigError("Configured paths must be strings")
    path = Path(value).expanduser()
    return (base / path).resolve()


def _memory(data: dict, base: Path) -> dict:
    section = data.get("memory", {})
    if not isinstance(section, dict):
        raise ConfigError("[memory] must be a table")
    settings = {**MEMORY_DEFAULTS, **section}
    if not isinstance(settings["enabled"], bool):
        raise ConfigError("memory.enabled must be a boolean")
    path = settings["path"]
    if not isinstance(path, str):
        raise ConfigError("memory.path must be a string")
    if path:
        settings["path"] = _path(path, base)
    for key in ("recall_probability", "recall_cooldown_min"):
        value = settings[key]
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            raise ConfigError(f"memory.{key} must be a finite nonnegative number")
    if settings["recall_probability"] > 1:
        raise ConfigError("memory.recall_probability must be between 0 and 1")
    return settings


def _flavor(data: dict) -> dict:
    section = data.get("flavor", {})
    if not isinstance(section, dict):
        raise ConfigError("[flavor] must be a table")
    settings = {**FLAVOR_DEFAULTS, **section}
    if not isinstance(settings["enabled"], bool):
        raise ConfigError("flavor.enabled must be a boolean")
    for key in ("probability", "cooldown_min", "item_cooldown_days"):
        value = settings[key]
        if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
            raise ConfigError(f"flavor.{key} must be a finite nonnegative number")
    if settings["probability"] > 1:
        raise ConfigError("flavor.probability must be between 0 and 1")
    return settings


USER_CONFIG = Path("~/.config/familiar/config.toml")


def find_config_path(path: str | Path | None = None) -> Path | None:
    """Explicit path, then $FAMILIAR_CONFIG, ./config.toml, the user config; None if absent."""
    selected = path or os.environ.get("FAMILIAR_CONFIG")
    if selected:
        return Path(selected).expanduser()
    candidates = [Path("config.toml"), USER_CONFIG.expanduser()]
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def load_config(path: str | Path | None = None) -> AppConfig:
    """Use an explicit path, environment override, local file, or user file."""
    source = find_config_path(path)
    data: dict = {}
    base = Path.cwd()
    if source is not None:
        try:
            with source.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"Cannot read config {source}: {exc}") from exc
        base = source.resolve().parent
    language = data.get("language", "en")
    persona = data.get("persona", "mira")
    if not isinstance(language, str) or not isinstance(persona, str):
        raise ConfigError("language and persona must be strings")
    game = data.get("game")
    if game is not None and (not isinstance(game, str) or not game):
        raise ConfigError("game must be a non-empty string")
    games = data.get("games", {})
    if not isinstance(games, dict) or any(not isinstance(v, dict) for v in games.values()):
        raise ConfigError("[games.<name>] must be a table")
    director = data.get("director", {})
    if not isinstance(director, dict):
        raise ConfigError("[director] must be a table")
    llm = _backend(data, "llm", "gemini")
    assert llm is not None
    return AppConfig(
        game=game,
        games=games,
        director=director,
        memory=_memory(data, base),
        flavor=_flavor(data),
        language=language,
        persona=persona,
        personas_dir=_path(data["personas_dir"], base) if "personas_dir" in data else None,
        user_personas_dir=_path(data.get("user_personas_dir", "~/.config/familiar/personas"), base),
        transcript_dir=_path(data.get("transcript_dir", "data/transcripts"), base),
        llm=llm,
        ptt=_backend(data, "ptt", "terminal"),
        stt=_backend(data, "stt"),
        tts=_backend(data, "tts"),
    )


def resolve_secret(settings: dict, key: str = "api_key_env") -> str:
    """Read a secret from the environment variable named in settings."""
    name = settings.get(key)
    if not isinstance(name, str) or not name:
        raise ConfigError(f"Missing environment variable name in {key}")
    secret = os.environ.get(name)
    if not secret:
        raise ConfigError(f"Environment variable {name!r} is missing or empty")
    return secret
