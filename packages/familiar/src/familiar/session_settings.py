"""Versioned launcher selections; no backend, secret or device settings.

Schema 2 adds optional stereo voice placement: ``stereo`` (off by default) and ``seats``
mapping persona ids to a pan from -1.0 (left) to 1.0 (right). Schema 3 adds an optional
``dj`` (the crew member running the music, ADR 0018), defaulting to the carrier. Older files
still load; saves always write version 3.
"""

import json
import os
import tempfile
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from familiar.interfaces import ConfigError
from familiar.persona import load_persona, validate_id

VERSION = 3
VERSIONS = (1, 2, 3)
_KEYS = {"version", "game", "crew", "language", "carrier", "dj", "banter", "stereo", "seats"}
_REQUIRED = {"version", "crew", "language", "carrier", "banter"}
SIDE_PAN = 0.7  # default seat for non-carrier members, alternating right then left


class SessionError(ConfigError):
    """All invalid selections in a session, reported together at Start."""


def default_seats(crew: object, carrier: object) -> dict[str, float]:
    """Carrier sits centre; others alternate +0.7 (right), -0.7 (left), as in the GUI."""
    seats: dict[str, float] = {}
    others = 0
    for member in _safe_crew(crew):
        if member == carrier:
            seats[member] = 0.0
            continue
        seats[member] = SIDE_PAN if others % 2 == 0 else -SIDE_PAN
        others += 1
    return seats


@dataclass(frozen=True)
class SessionSettings:
    crew: tuple[str, ...]
    language: str
    carrier: str
    game: str | None = None
    banter: int = 0
    stereo: bool = False
    seats: dict[str, float] | None = None
    version: int = VERSION
    dj: str | None = None

    def __post_init__(self) -> None:
        if self.dj is None:
            object.__setattr__(self, "dj", self.carrier)
        if self.stereo is None:
            object.__setattr__(self, "stereo", False)
        seats = default_seats(self.crew, self.carrier)
        if isinstance(self.seats, dict):
            seats.update(self.seats)
        object.__setattr__(self, "seats", seats)


def default_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "familiar/session.toml"


def _problems(data: dict) -> list[str]:
    problems = [f"Unknown session key: {key}" for key in sorted(data.keys() - _KEYS)]
    problems += [f"Missing session key: {key}" for key in sorted(_REQUIRED - data.keys())]
    if type(data.get("version")) is not int or data["version"] not in VERSIONS:
        problems.append("Session version must be 1, 2 or 3")
    game = data.get("game")
    if game is not None and (not isinstance(game, str) or not game.strip()):
        problems.append("game must be a non-empty string or absent (desk mode)")
    if not isinstance(data.get("language"), str) or not data["language"].strip():
        problems.append("language must be a non-empty string")
    if type(data.get("banter")) is not int or not 0 <= data["banter"] <= 10:
        problems.append("banter must be an integer from 0 to 10")
    problems.extend(_stereo_problems(data))
    crew = data.get("crew")
    problems.extend(_crew_problems(crew, data.get("carrier")))
    dj = data.get("dj")
    if dj is not None and (not isinstance(dj, str) or dj not in _safe_crew(crew)):
        problems.append("dj must be a member of crew")
    return problems


def _stereo_problems(data: dict) -> list[str]:
    problems = []
    stereo = data.get("stereo")
    if stereo is not None and type(stereo) is not bool:
        problems.append("stereo must be true or false")
    seats = data.get("seats")
    if seats is not None and not isinstance(seats, dict):
        problems.append("seats must map persona ids to pan values")
    if not isinstance(seats, dict):
        return problems
    members = set(_safe_crew(data.get("crew")))
    for key, pan in seats.items():
        if not isinstance(key, str) or key not in members:
            problems.append(f"seats key {key!r} is not a crew member")
            continue
        if isinstance(pan, bool) or not isinstance(pan, (int, float)):
            problems.append(f"seats pan for {key!r} must be a number from -1.0 to 1.0")
            continue
        if not -1.0 <= pan <= 1.0:
            problems.append(f"seats pan for {key!r} must be a number from -1.0 to 1.0")
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
    # Overrides may shrink the crew; drop seats of members no longer aboard (raw file
    # problems were already reported against the file's own crew).
    seats = effective.get("seats")
    if isinstance(seats, dict):
        seats = {key: pan for key, pan in seats.items() if key in _safe_crew(crew)}
    settings = SessionSettings(
        crew=_safe_crew(crew),
        language=effective.get("language", ""),
        carrier=effective.get("carrier", ""),
        dj=effective.get("dj"),
        game=effective.get("game"),
        banter=effective.get("banter", 0),
        stereo=effective.get("stereo", False),
        seats=seats,
        version=VERSION,
    )
    try:
        validate(settings, personas_dir, user_personas_dir)
    except SessionError as exc:
        problems.extend(str(exc).removeprefix("Invalid session:\n- ").split("\n- "))
    if problems:
        raise SessionError("Invalid session:\n- " + "\n- ".join(dict.fromkeys(problems)))
    return settings


def _toml_value(key: str, value: object) -> str:
    if key == "seats" and isinstance(value, dict):
        items = ", ".join(f"{seat} = {json.dumps(pan)}" for seat, pan in value.items())
        return "{ " + items + " }" if items else "{}"
    return json.dumps(value, ensure_ascii=False)


def save(path: str | Path, settings: SessionSettings) -> None:
    """Replace the last selection atomically; TOML represents null game by omission."""
    data = asdict(settings)
    data["version"] = VERSION  # saves always write the current schema
    problems = _problems(data)
    if problems:
        raise SessionError("Invalid session:\n- " + "\n- ".join(problems))
    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"{key} = {_toml_value(key, value)}" for key, value in data.items() if value is not None
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
