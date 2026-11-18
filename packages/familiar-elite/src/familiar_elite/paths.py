"""Rescannable Steam/Proton path discovery with user-overridable defaults."""

import re
from dataclasses import dataclass
from itertools import chain
from pathlib import Path

from familiar.interfaces import load_data


@dataclass(frozen=True)
class ElitePaths:
    journal_dir: Path | None = None
    bindings_dir: Path | None = None
    control_schemes_dir: Path | None = None


def _libraries(root: Path) -> list[Path]:
    try:
        text = (root / "steamapps/libraryfolders.vdf").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    # Current VDF uses path keys; older Steam versions use numbered string values.
    entries = re.findall(r'"(?:path|[0-9]+)"\s+"([^"\n]+)"', text)
    return [Path(value.replace("\\\\", "\\")).expanduser() for value in entries]


def find_paths(settings: dict) -> ElitePaths:
    data = load_data("familiar_elite", "paths").value
    roots = [Path(value).expanduser() for value in data["steam_roots"]]
    libraries = list(dict.fromkeys(chain(roots, chain.from_iterable(map(_libraries, roots)))))
    return ElitePaths(
        **{
            key: _find(key, settings, data, libraries)
            for key in ("journal_dir", "bindings_dir", "control_schemes_dir")
        }
    )


def _find(key: str, settings: dict, data: dict, libraries: list[Path]) -> Path | None:
    if settings.get(key):
        return Path(settings[key]).expanduser()
    if key == "control_schemes_dir":
        matches = (sorted(library.glob(data["control_schemes_glob"])) for library in libraries)
        candidates = [path for path in chain.from_iterable(matches) if path.is_dir()]
        candidates.sort(key=lambda path: path.parent.name != data["preferred_product"])
        return next(iter(candidates), None)
    suffix = Path(data["proton_user_dir"].format(appid=data["appid"]))
    candidates = (library / suffix / data[key] for library in libraries)
    return next((path for path in candidates if path.is_dir()), None)
