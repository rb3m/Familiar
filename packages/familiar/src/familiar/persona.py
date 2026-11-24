"""Persona data, language-specific voices, and system prompt construction."""

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from familiar.interfaces import ConfigError, load_data
from familiar.speech import MARKUP_RULE

BUILTIN_PERSONAS_DIR = Path(__file__).parent / "personas"


def validate_language(code: object, entry: object) -> None:
    """Validate catalog entries without imposing a fixed language inventory."""
    if not isinstance(code, str) or not re.fullmatch(
        r"[a-zA-Z]{2,3}(?:-[A-Z]{2}|-[0-9]{3})?", code
    ):
        raise ConfigError(f"Invalid language code {code!r}; use a code such as en or pt-BR")
    if not isinstance(entry, dict):
        raise ConfigError(f"Language {code!r} must be a mapping")
    for key in ("name", "native"):
        if not isinstance(entry.get(key), str) or not entry[key].strip():
            raise ConfigError(f"Language {code!r} {key} must be a non-empty string")


def languages() -> dict[str, dict[str, str]]:
    """Read and validate the packaged language catalog and user changes."""
    dataset = load_data("familiar", "languages")
    try:
        if not isinstance(dataset.value, dict):
            raise ConfigError("Languages must be a mapping")
        for code, entry in dataset.value.items():
            validate_language(code, entry)
    except ConfigError as exc:
        raise ConfigError(f"{exc} in {', '.join(dataset.sources)}") from exc
    return dataset.value


def language_names() -> dict[str, str]:
    return {code: entry["name"] for code, entry in languages().items()}


# How loosely a persona may treat facts. Practical information is always exact: a companion
# who exaggerates the fuel level or a deadline stops being fun and starts being a hazard.
PRACTICAL_FACTS_RULE = (
    "Practical information is always exact and never embellished: fuel, damage, speed, "
    "distances, directions, deadlines, time and anything else the player may act on. Only "
    "state such numbers when you were given them; never estimate, calculate or guess one "
    "(no range from a fuel percentage, no arrival time from a distance). If it wasn't given, "
    "say you don't know."
)
SPOKEN_RULE = (
    "Your replies are spoken aloud by a text-to-speech voice. Write units and abbreviations as "
    "full words exactly as given in the data (ly as light-years, km/h as kilometres per hour, "
    "mph as miles per hour); never convert to another unit, and avoid symbols that read "
    "badly aloud."
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
    aliases: dict[str, list[str]] = field(default_factory=dict)
    interests: dict[str, float] = field(default_factory=dict)
    relationships: dict[str, str] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)
    # Free-text likes per topic, e.g. {"music": ["70s city pop", "quiet folk"]}.
    tastes: dict[str, list[str]] = field(default_factory=dict)

    topics: list[str] = field(default_factory=list)

    def names(self, language: str) -> list[str]:
        """The display name is an alias in every language."""
        return list(dict.fromkeys([self.display_name, *self.aliases.get(language, [])]))

    def system_prompt(self, language: str, game: str | None = None) -> str:
        """Combine character, style, game context, and response language."""
        if language not in self.languages:
            raise ConfigError(f"Persona {self.id!r} does not support language {language!r}")
        parts = [self.prompt.strip()]
        parts.extend(f"- {rule}" for rule in self.style_rules)
        parts.append(f"- {TRUTHFULNESS_RULES[self.truthfulness]}")
        parts.append(f"- {PRACTICAL_FACTS_RULE}")
        parts.append(f"- {SPOKEN_RULE}")
        parts.append(f"- {MARKUP_RULE}")
        if game in self.game_hooks:
            parts.append(self.game_hooks[game])
        parts.append(f"Always reply in {language_names().get(language, language)}.")
        return "\n".join(parts)

    def voice(self, tts_backend: str, language: str) -> str | None:
        """Return a backend voice, or None when none is configured."""
        return self.languages.get(language, {}).get("voices", {}).get(tts_backend)

    def respellings(self, language: str) -> dict[str, str]:
        """Written word -> form the voice should read (familiar.respell); empty when none."""
        return self.languages.get(language, {}).get("spoken", {})


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
        spoken = settings.get("spoken", {})
        if not isinstance(spoken, dict) or any(
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(value, str)
            or not value.strip()
            for key, value in spoken.items()
        ):
            raise ConfigError("Persona spoken must map written words to non-empty spoken forms")
    hooks = data.get("game_hooks", {})
    if not isinstance(hooks, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in hooks.items()
    ):
        raise ConfigError("Persona game_hooks must map game names to strings")
    truthfulness = data.get("truthfulness", "strict")
    if not isinstance(truthfulness, str) or truthfulness not in TRUTHFULNESS_RULES:
        allowed = ", ".join(TRUTHFULNESS_RULES)
        raise ConfigError(f"Persona truthfulness must be one of: {allowed}")
    aliases = data.get("aliases", {})
    if not isinstance(aliases, dict) or any(
        not isinstance(lang, str)
        or not isinstance(names, list)
        or any(not isinstance(name, str) or not name.strip() for name in names)
        for lang, names in aliases.items()
    ):
        raise ConfigError("Persona aliases must map languages to lists of non-empty names")
    interests = data.get("interests", {})
    if not isinstance(interests, dict) or any(
        not isinstance(kind, str)
        or not kind
        or type(weight) not in {int, float}
        or not math.isfinite(weight)
        or weight < 0
        for kind, weight in interests.items()
    ):
        raise ConfigError("Persona interests must map event kinds/prefixes to nonnegative weights")
    relationships = _one_line_mapping(data, "relationships", "persona ids")
    for identifier in relationships:
        validate_id(identifier)
    roles = _one_line_mapping(data, "roles", "game ids")
    tastes = data.get("tastes", {})
    if not isinstance(tastes, dict) or any(
        not isinstance(topic, str)
        or not isinstance(likes, list)
        or any(not isinstance(like, str) or not like.strip() for like in likes)
        for topic, likes in tastes.items()
    ):
        raise ConfigError("Persona tastes must map topics to lists of non-empty strings")
    topics = data.get("topics", [])
    if not isinstance(topics, list) or any(
        not isinstance(topic, str) or not topic.strip() for topic in topics
    ):
        raise ConfigError("Persona topics must be a list of non-empty strings")
    return {
        **data,
        "topics": topics,
        "style_rules": rules,
        "game_hooks": hooks,
        "truthfulness": truthfulness,
        "aliases": aliases,
        "interests": interests,
        "relationships": relationships,
        "roles": roles,
        "tastes": tastes,
    }


def _one_line_mapping(data: dict, field_name: str, keys: str) -> dict[str, str]:
    """Validate an optional {key: one-line text} mapping such as relationships or roles."""
    value = data.get(field_name, {})
    message = f"Persona {field_name} must map {keys} to non-empty one-line strings"
    if not isinstance(value, dict):
        raise ConfigError(message)
    invalid = [
        key
        for key, text in value.items()
        if not isinstance(key, str)
        or not key
        or not isinstance(text, str)
        or not text.strip()
        or any(char in text for char in "\r\n")
    ]
    if invalid:
        raise ConfigError(message)
    return value


_validate = validate_persona


def load_persona(
    personas_dir: str | Path | None,
    persona_id: str,
    user_personas_dir: str | Path = "~/.config/familiar/personas",
) -> Persona:
    """Load and validate a persona YAML file from its directory."""
    validate_id(persona_id)
    paths = persona_directories(user_personas_dir, personas_dir)
    path = next(
        (
            directory / f"{persona_id}.yaml"
            for directory in paths
            if (directory / f"{persona_id}.yaml").is_file()
        ),
        BUILTIN_PERSONAS_DIR / f"{persona_id}.yaml",
    )
    try:
        data = _validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read persona {path}: {exc}") from exc
    if data["id"] != persona_id:
        raise ConfigError(
            f"Persona id {data['id']!r} does not match file id {persona_id!r}: {path}"
        )
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


def persona_directories(user_dir: str | Path, configured_dir: str | Path | None) -> list[Path]:
    """Search user overrides, optional configured personas, then packaged defaults."""
    return [
        Path(user_dir).expanduser(),
        *([Path(configured_dir).expanduser()] if configured_dir is not None else []),
        BUILTIN_PERSONAS_DIR,
    ]
