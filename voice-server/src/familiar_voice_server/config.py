"""Validated server configuration; no model runtime dependencies."""

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """An invalid configuration, with its offending field in the message."""


@dataclass(frozen=True)
class ListenConfig:
    host: str = "0.0.0.0"
    port: int = 8765


@dataclass(frozen=True)
class LeaseConfig:
    ttl_s: float = 30
    grace_s: float = 60


@dataclass(frozen=True)
class WorkerConfig:
    stop_timeout_s: float = 10


@dataclass(frozen=True)
class ModelConfig:
    id: str
    streaming_interval_s: float = 0.32


@dataclass(frozen=True)
class VoiceConfig:
    mode: str
    ref_audio: Path | None = None
    ref_text: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class Config:
    path: Path
    listen: ListenConfig
    token_env: str
    lease: LeaseConfig
    worker: WorkerConfig
    model: ModelConfig
    voices: dict[str, VoiceConfig]
    languages: dict[str, str]

    def speech(self, body: Any) -> dict[str, str]:
        """Validate a public or private speech request before reserving the engine."""
        if not isinstance(body, dict):
            raise ValueError("speech body must be an object")
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        voice = body.get("voice")
        if not isinstance(voice, str) or voice not in self.voices:
            raise ValueError("unknown voice")
        language = body.get("language")
        if not isinstance(language, str) or language not in self.languages:
            raise ValueError("unknown language")
        return {"text": text, "voice": voice, "language": language}


def _mapping(value: Any, field: str, keys: set[str] | None = None) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{field} must be a mapping")
    if keys is not None and value.keys() - keys:
        raise ConfigError(f"{field}: unknown fields {sorted(value.keys() - keys, key=str)}")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} must be a non-empty string")
    return value


def _seconds(value: Any, field: str, *, zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{field} must be a finite number")
    if not math.isfinite(value) or value < 0 or (value == 0 and not zero):
        raise ConfigError(f"{field} must be {'non-negative' if zero else 'positive'} and finite")
    return float(value)


def _voice(value: Any, name: str, directory: Path) -> VoiceConfig:
    field = f"voices.{name}"
    data = _mapping(value, field, {"mode", "ref_audio", "ref_text", "description"})
    mode = data.get("mode")
    if mode == "design":
        return VoiceConfig(
            mode=mode, description=_string(data.get("description"), f"{field}.description")
        )
    if mode != "clone":
        raise ConfigError(f"{field}.mode must be clone or design")
    audio = Path(_string(data.get("ref_audio"), f"{field}.ref_audio")).expanduser()
    return VoiceConfig(
        mode=mode,
        ref_audio=(directory / audio).resolve(),
        ref_text=_string(data.get("ref_text"), f"{field}.ref_text"),
    )


def load_config(path: str | Path) -> Config:
    path = Path(path).expanduser().resolve()
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read config {path}: {exc}") from exc
    data = _mapping(
        data, "config", {"listen", "token_env", "lease", "worker", "model", "voices", "languages"}
    )
    listen = _mapping(data.get("listen", {}), "listen", {"host", "port"})
    port = listen.get("port", 8765)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ConfigError("listen.port must be an integer from 1 to 65535")
    lease = _mapping(data.get("lease", {}), "lease", {"ttl_s", "grace_s"})
    worker = _mapping(data.get("worker", {}), "worker", {"stop_timeout_s"})
    model = _mapping(data.get("model"), "model", {"id", "streaming_interval_s"})
    voices = _mapping(data.get("voices"), "voices")
    if not voices:
        raise ConfigError("voices must contain at least one voice")
    parsed_voices = {
        _string(name, "voice id"): _voice(value, str(name), path.parent)
        for name, value in voices.items()
    }
    languages = _mapping(
        data.get("languages", {"en": "english", "es": "spanish", "ja": "japanese"}), "languages"
    )
    if not languages:
        raise ConfigError("languages must contain at least one language")
    return Config(
        path=path,
        listen=ListenConfig(_string(listen.get("host", "0.0.0.0"), "listen.host"), port),
        token_env=_string(data.get("token_env", "FAMILIAR_VOICE_TOKEN"), "token_env"),
        lease=LeaseConfig(
            _seconds(lease.get("ttl_s", 30), "lease.ttl_s"),
            _seconds(lease.get("grace_s", 60), "lease.grace_s", zero=True),
        ),
        worker=WorkerConfig(_seconds(worker.get("stop_timeout_s", 10), "worker.stop_timeout_s")),
        model=ModelConfig(
            _string(model.get("id"), "model.id"),
            _seconds(model.get("streaming_interval_s", 0.32), "model.streaming_interval_s"),
        ),
        voices=parsed_voices,
        languages={
            _string(k, "language code"): _string(v, f"languages.{k}") for k, v in languages.items()
        },
    )
