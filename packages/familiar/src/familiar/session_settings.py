"""Version-one launcher selections; no backend, secret or device settings."""

import json
import os
import tempfile
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from familiar.interfaces import ConfigError
from familiar.persona import load_persona, validate_id

VERSION = 1
_KEYS = {"version", "game", "crew", "language", "carrier", "banter"}


class SessionError(ConfigError):
    """All invalid selections in a session, reported together at Start."""


@dataclass(frozen=True)
class SessionSettings:
    crew: tuple[str, ...]
    language: str
    carrier: str
    game: str | None = None
    banter: int = 0
    version: int = VERSION


def default_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "familiar/session.toml"


def _problems(data: dict) -> list[str]:
    problems = [f"Unknown session key: {key}" for key in sorted(data.keys() - _KEYS)]
    problems += [f"Missing session key: {key}" for key in sorted(_KEYS - {"game"} - data.keys())]
    if type(data.get("version")) is not int or data["version"] != VERSION:
        problems.append("Session version must be 1")
    game = data.get("game")
    if game is not None and (not isinstance(game, str) or not game.strip()):
        problems.append("game must be a non-empty string or absent (desk mode)")
    if not isinstance(data.get("language"), str) or not data["language"].strip():
        problems.append("language must be a non-empty string")
    if type(data.get("banter")) is not int or not 0 <= data["banter"] <= 10:
        problems.append("banter must be an integer from 0 to 10")
    crew = data.get("crew")
    problems.extend(_crew_problems(crew, data.get("carrier")))
    return problems


def _crew_problems(crew: object, carrier: object) -> list[str]:
    problems = []
    if not isinstance(crew, (list, tuple)) or not 1 <= len(crew) <= 3:
        problems.append("crew must contain 1-3 unique persona ids in roster order")
    if not isinstance(crew, (list, tuple)):
        problems.append("carrier must be a member of crew")
        return problems
    ids = [item for item in crew if isinstance(item, str)]
    if len(set(ids)) != len(crew):
        problems.append("crew must contain unique string persona ids")
    for identifier in crew:
        try:
            validate_id(identifier)
        except ConfigError as exc:
            problems.append(str(exc))
    if not isinstance(carrier, str) or carrier not in crew:
        problems.append("carrier must be a member of crew")
    return problems


def _voice_problems(identifier: str, language: object, personas_dir, options: dict) -> list[str]:
    try:
        persona = load_persona(personas_dir, identifier, **options)
    except ConfigError as exc:
        return [str(exc)]
    if not isinstance(language, str):
        return []  # The schema already reports an invalid language.
    voices = persona.languages.get(language, {}).get("voices", {})
    if any(isinstance(voice, str) and voice.strip() for voice in voices.values()):
        return []
    return [f"Persona {identifier!r} has no voices for {language!r}"]


def validate(settings: SessionSettings, personas_dir=None, user_personas_dir=None) -> None:
    """Validate the whole roster, including every unavailable language voice."""
    data = asdict(settings)
    problems = _problems(data)
    options = {}
    if user_personas_dir is not None:
        options["user_personas_dir"] = user_personas_dir
    for identifier in _safe_crew(settings.crew):
        problems.extend(_voice_problems(identifier, settings.language, personas_dir, options))
    if problems:
        raise SessionError("Invalid session:\n- " + "\n- ".join(problems))


def _safe_crew(crew: object) -> tuple[str, ...]:
    if not isinstance(crew, (list, tuple)):
        return ()
    return tuple(item for item in crew if isinstance(item, str))


def load(
    path: str | Path, *, personas_dir=None, user_personas_dir=None, overrides: dict | None = None
) -> SessionSettings:
    try:
        with Path(path).expanduser().open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SessionError(f"Cannot read session {path}: {exc}") from exc
    problems = _problems(data)
    effective = {**data, **(overrides or {})}
    # Collect persona errors even when other fields are malformed.
    crew = effective.get("crew", [])
    settings = SessionSettings(
        crew=_safe_crew(crew),
        language=effective.get("language", ""),
        carrier=effective.get("carrier", ""),
        game=effective.get("game"),
        banter=effective.get("banter", 0),
        version=effective.get("version", 0),
    )
    try:
        validate(settings, personas_dir, user_personas_dir)
    except SessionError as exc:
        problems.extend(str(exc).removeprefix("Invalid session:\n- ").split("\n- "))
    if problems:
        raise SessionError("Invalid session:\n- " + "\n- ".join(dict.fromkeys(problems)))
    return settings


def save(path: str | Path, settings: SessionSettings) -> None:
    """Replace the last selection atomically; TOML represents null game by omission."""
    data = asdict(settings)
    problems = _problems(data)
    if problems:
        raise SessionError("Invalid session:\n- " + "\n- ".join(problems))
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"{key} = {json.dumps(value, ensure_ascii=False)}"
        for key, value in data.items()
        if value is not None
    ]
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
