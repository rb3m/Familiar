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
from familiar.session_settings import SessionSettings
from familiar.session_settings import default_path as default_session_path
from familiar.session_settings import load as load_session

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


ALERT_DEFAULTS = {"enabled": True, "lead_s": 1.0, "max_alerts": 8}


MUSIC_DEFAULTS = {"enabled": False, "service": "pear"}

DESK_DEFAULTS = {
    "enabled": False,
    "interval_s": 60,
    "latitude": None,
    "longitude": None,
    "timezone": None,
    "location": None,
    "location_notes": None,
    "sources": ["clock"],
}


HANDS_DEFAULTS = {
    "enabled": True,
    "keyboard": "uinput",
    "confirm_ttl_s": 30,
    "action_timeout_s": 5,
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
    alerts: dict = field(default_factory=lambda: ALERT_DEFAULTS.copy())
    hands: dict = field(default_factory=lambda: HANDS_DEFAULTS.copy())
    music: dict = field(default_factory=lambda: MUSIC_DEFAULTS.copy())
    desk: dict = field(default_factory=lambda: DESK_DEFAULTS.copy())
    config_path: Path | None = None


def session_settings(
    config: AppConfig,
    *,
    path: str | Path | None = None,
    persona: str | None = None,
    game: str | None = None,
    language: str | None = None,
    use_session: bool = True,
) -> SessionSettings:
    """CLI/env selections override a saved session, then ordinary config defaults."""
    overrides = {}
    if persona:
        overrides.update(crew=(persona,), carrier=persona, dj=persona)
    if game:
        overrides["game"] = game
    if language:
        overrides["language"] = language
    selected = default_session_path()
    if path is not None:
        selected = Path(path).expanduser()
    if use_session and (path is not None or selected.exists()):
        return load_session(
            selected,
            personas_dir=config.personas_dir,
            user_personas_dir=config.user_personas_dir,
            overrides=overrides,
        )
    return SessionSettings(
        **{
            "crew": (config.persona,),
            "carrier": config.persona,
            "game": config.game,
            "language": config.language,
            **overrides,
        }
    )


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


def _alerts(data: dict) -> dict:
    section = data.get("alerts", {})
    if not isinstance(section, dict):
        raise ConfigError("[alerts] must be a table")
    settings = {**ALERT_DEFAULTS, **section}
    if not isinstance(settings["enabled"], bool):
        raise ConfigError("alerts.enabled must be a boolean")
    lead = settings["lead_s"]
    if type(lead) not in {int, float} or not math.isfinite(lead) or lead < 0:
        raise ConfigError("alerts.lead_s must be a finite nonnegative number")
    maximum = settings["max_alerts"]
    if type(maximum) is not int or not 1 <= maximum <= 8:
        raise ConfigError("alerts.max_alerts must be an integer between 1 and 8")
    return settings


def _music(data: dict) -> dict:
    section = data.get("music", {})
    if not isinstance(section, dict):
        raise ConfigError("[music] must be a table")
    settings = {**MUSIC_DEFAULTS, **section}
    if type(settings["enabled"]) is not bool:
        raise ConfigError("music.enabled must be a boolean")
    if not isinstance(settings["service"], str) or not settings["service"]:
        raise ConfigError("music.service must be a non-empty string")
    return settings


def _desk(data: dict) -> dict:
    section = data.get("desk", {})
    if not isinstance(section, dict):
        raise ConfigError("[desk] must be a table")
    settings = {**DESK_DEFAULTS, **section}
    if type(settings["enabled"]) is not bool:
        raise ConfigError("desk.enabled must be a boolean")
    for key in ("interval_s", "poll_s"):
        value = settings.get(key, 5)
        if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
            raise ConfigError(f"desk.{key} must be a finite positive number")
    for key, limit in (("latitude", 90), ("longitude", 180)):
        value = settings[key]
        if value is None:
            continue
        if type(value) not in {int, float} or not math.isfinite(value) or abs(value) > limit:
            raise ConfigError(f"desk.{key} must be between {-limit} and {limit}")
    if (settings["latitude"] is None) != (settings["longitude"] is None):
        raise ConfigError("desk.latitude and desk.longitude must be set together")
    sources = settings["sources"]
    if not isinstance(sources, list) or not all(isinstance(s, str) and s for s in sources):
        raise ConfigError("desk.sources must be a list of source names")
    if not isinstance(settings.get("weather", {}), dict):
        raise ConfigError("[desk.weather] must be a table")
    for key in ("location", "location_notes"):
        value = settings[key]
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ConfigError(f"desk.{key} must be non-empty text")
    zone = settings["timezone"]
    if zone is not None:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        if not isinstance(zone, str) or not zone:
            raise ConfigError("desk.timezone must be an IANA timezone")
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError("desk.timezone must be an IANA timezone") from exc
    return settings


def _hands(data: dict) -> dict:
    section = data.get("hands", {})
    if not isinstance(section, dict):
        raise ConfigError("[hands] must be a table")
    settings = {**HANDS_DEFAULTS, **section}
    if not isinstance(settings["enabled"], bool):
        raise ConfigError("hands.enabled must be a boolean")
    if not isinstance(settings["keyboard"], str) or not settings["keyboard"]:
        raise ConfigError("hands.keyboard must be a non-empty string")
    for key in ("confirm_ttl_s", "action_timeout_s"):
        value = settings[key]
        if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
            raise ConfigError(f"hands.{key} must be a finite positive number")
    for key in ("affirmatives", "negatives"):
        words = settings.get(key, {})
        if not isinstance(words, dict) or any(
            not isinstance(lang, str)
            or not isinstance(entries, list)
            or any(not isinstance(word, str) for word in entries)
            for lang, entries in words.items()
        ):
            raise ConfigError(f"hands.{key} must be a table of string lists")
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
        alerts=_alerts(data),
        hands=_hands(data),
        music=_music(data),
        desk=_desk(data),
        config_path=source.resolve() if source is not None else None,
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
