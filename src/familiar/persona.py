"""Persona data, language-specific voices, and system prompt construction."""

import math
from dataclasses import dataclass
from pathlib import Path

import yaml

from familiar.interfaces import ConfigError

LANGUAGE_NAMES = {"en": "English", "es": "Spanish", "ja": "Japanese"}


@dataclass(frozen=True)
class Persona:
    id: str
    display_name: str
    source: str
    chattiness: float
    prompt: str
    style_rules: list[str]
    languages: dict
    game_hooks: dict[str, str]

    def system_prompt(self, language: str, game: str | None = None) -> str:
        """Combine character, style, game context, and response language."""
        if language not in self.languages:
            raise ConfigError(f"Persona {self.id!r} does not support language {language!r}")
        parts = [self.prompt.strip()]
        parts.extend(f"- {rule}" for rule in self.style_rules)
        if game in self.game_hooks:
            parts.append(self.game_hooks[game])
        parts.append(f"Always reply in {LANGUAGE_NAMES.get(language, language)}.")
        return "\n".join(parts)

    def voice(self, tts_backend: str, language: str) -> str | None:
        """Return a backend voice, or None when none is configured."""
        return self.languages.get(language, {}).get("voices", {}).get(tts_backend)


def _validate(data: object) -> dict:
    if not isinstance(data, dict):
        raise ConfigError("Persona must be a YAML mapping")
    for key in ("id", "display_name", "source", "prompt"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ConfigError(f"Persona {key} must be a non-empty string")
    chattiness = data.get("chattiness")
    if (
        isinstance(chattiness, bool)
        or not isinstance(chattiness, (int, float))
        or not math.isfinite(chattiness)
        or not 0 <= chattiness <= 1
    ):
        raise ConfigError("Persona chattiness must be between 0 and 1")
    rules = data.get("style_rules", [])
    if not isinstance(rules, list) or any(not isinstance(rule, str) for rule in rules):
        raise ConfigError("Persona style_rules must be a list of strings")
    languages = data.get("languages")
    if not isinstance(languages, dict) or not languages:
        raise ConfigError("Persona languages must be a non-empty mapping")
    for language, settings in languages.items():
        if not isinstance(language, str) or not isinstance(settings, dict):
            raise ConfigError("Persona language entries must be mappings")
        voices = settings.get("voices", {})
        if not isinstance(voices, dict) or any(
            not isinstance(key, str) or (value is not None and not isinstance(value, str))
            for key, value in voices.items()
        ):
            raise ConfigError("Persona voices must map backend names to strings or null")
    hooks = data.get("game_hooks", {})
    if not isinstance(hooks, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in hooks.items()
    ):
        raise ConfigError("Persona game_hooks must map game names to strings")
    return {**data, "style_rules": rules, "game_hooks": hooks}


def load_persona(personas_dir: str | Path, persona_id: str) -> Persona:
    """Load and validate a persona YAML file from its directory."""
    if Path(persona_id).name != persona_id or persona_id in {"", ".", ".."}:
        raise ConfigError(f"Invalid persona id {persona_id!r}")
    path = Path(personas_dir) / f"{persona_id}.yaml"
    try:
        data = _validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read persona {path}: {exc}") from exc
    return Persona(**{key: data[key] for key in Persona.__dataclass_fields__})
