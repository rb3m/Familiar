"""Persona data, language-specific voices, and system prompt construction."""

import math
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from familiar.interfaces import ConfigError

LANGUAGE_NAMES = {"en": "English", "es": "Spanish", "ja": "Japanese"}

# How loosely a persona may treat facts. Practical information is always exact: a companion
# who exaggerates the fuel level or a deadline stops being fun and starts being a hazard.
PRACTICAL_FACTS_RULE = (
    "Practical information is always exact and never embellished: fuel, damage, speed, "
    "distances, directions, deadlines, time and anything else the player may act on. Only "
    "state such numbers when you were given them; never estimate, calculate or guess one "
    "(no range from a fuel percentage, no arrival time from a distance). If it wasn't given, "
    "say you don't know."
)
TRUTHFULNESS_RULES = {
    "strict": (
        "Never invent facts about places, history or the game. Use only facts you are given or "
        "are sure of; if you don't know, say so plainly."
    ),
    "playful": (
        "When talking about places, landmarks and lore you may exaggerate wildly, boast and spin "
        "grand legends, as long as it is obviously hyperbole in your own voice. If the player "
        "sincerely asks whether something is true, answer straight with only facts you are given "
        "or are sure of."
    ),
}


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
    truthfulness: str = "strict"

    def system_prompt(self, language: str, game: str | None = None) -> str:
        """Combine character, style, game context, and response language."""
        if language not in self.languages:
            raise ConfigError(f"Persona {self.id!r} does not support language {language!r}")
        parts = [self.prompt.strip()]
        parts.extend(f"- {rule}" for rule in self.style_rules)
        parts.append(f"- {TRUTHFULNESS_RULES[self.truthfulness]}")
        parts.append(f"- {PRACTICAL_FACTS_RULE}")
        if game in self.game_hooks:
            parts.append(self.game_hooks[game])
        parts.append(f"Always reply in {LANGUAGE_NAMES.get(language, language)}.")
        return "\n".join(parts)

    def voice(self, tts_backend: str, language: str) -> str | None:
        """Return a backend voice, or None when none is configured."""
        return self.languages.get(language, {}).get("voices", {}).get(tts_backend)


def validate_id(persona_id: object) -> None:
    """Reject IDs that cannot safely name a persona file."""
    if not isinstance(persona_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", persona_id):
        raise ConfigError(f"Invalid persona id {persona_id!r}")


def validate_persona(data: object) -> dict:
    if not isinstance(data, dict):
        raise ConfigError("Persona must be a YAML mapping")
    for key in ("id", "display_name", "source", "prompt"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ConfigError(f"Persona {key} must be a non-empty string")
    chattiness = data.get("chattiness")
    validate_id(data["id"])
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
    truthfulness = data.get("truthfulness", "strict")
    if not isinstance(truthfulness, str) or truthfulness not in TRUTHFULNESS_RULES:
        allowed = ", ".join(TRUTHFULNESS_RULES)
        raise ConfigError(f"Persona truthfulness must be one of: {allowed}")
    return {**data, "style_rules": rules, "game_hooks": hooks, "truthfulness": truthfulness}


_validate = validate_persona


def load_persona(
    personas_dir: str | Path,
    persona_id: str,
    user_personas_dir: str | Path = "~/.config/familiar/personas",
) -> Persona:
    """Load and validate a persona YAML file from its directory."""
    validate_id(persona_id)
    paths = [Path(user_personas_dir).expanduser(), Path(personas_dir)]
    path = next(
        (
            directory / f"{persona_id}.yaml"
            for directory in paths
            if (directory / f"{persona_id}.yaml").is_file()
        ),
        Path(personas_dir) / f"{persona_id}.yaml",
    )
    try:
        data = _validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read persona {path}: {exc}") from exc
    return Persona(**{key: data[key] for key in Persona.__dataclass_fields__})


def list_personas(dirs: list[Path]) -> list[tuple[Persona, bool]]:
    """List user then builtin directories, with the first ID taking precedence."""
    found: dict[str, tuple[Persona, bool]] = {}
    for index, directory in enumerate(dirs):
        for path in sorted(Path(directory).expanduser().glob("*.yaml")):
            if path.stem in found:
                continue
            persona = load_persona(path.parent, path.stem, path.parent)
            found[persona.id] = (persona, index > 0)
    return [found[key] for key in sorted(found)]
