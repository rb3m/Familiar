"""Load application settings, resolving paths relative to their TOML file."""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from familiar.interfaces import ConfigError


@dataclass
class BackendConfig:
    backend: str
    settings: dict = field(default_factory=dict)
    fallback: "BackendConfig | None" = None


@dataclass
class AppConfig:
    language: str = "en"
    persona: str = "mira"
    personas_dir: Path = Path("personas")
    transcript_dir: Path = Path("data/transcripts")
    llm: BackendConfig = field(default_factory=lambda: BackendConfig("gemini"))
    ptt: BackendConfig = field(default_factory=lambda: BackendConfig("terminal"))
    stt: BackendConfig | None = None
    tts: BackendConfig | None = None


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


def load_config(path: str | Path | None = None) -> AppConfig:
    """Use an explicit path, environment override, local file, or user file."""
    selected = path or os.environ.get("FAMILIAR_CONFIG")
    candidates = [Path("config.toml"), Path("~/.config/familiar/config.toml").expanduser()]
    source = (
        Path(selected).expanduser()
        if selected
        else next((candidate for candidate in candidates if candidate.is_file()), None)
    )
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
    llm = _backend(data, "llm", "gemini")
    assert llm is not None
    return AppConfig(
        language=language,
        persona=persona,
        personas_dir=_path(data.get("personas_dir", "personas"), base),
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
