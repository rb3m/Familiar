"""Validated local landmark packs; no network lookups."""

from collections.abc import Iterable
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from itertools import chain
from math import isfinite
from pathlib import Path
from urllib.parse import urlparse

import yaml

from familiar.interfaces import ConfigError

from .geo import haversine


@dataclass(frozen=True)
class Landmark:
    id: str
    name: str
    state: str
    lat: float
    lon: float
    radius_km: float
    kind: str
    facts: tuple[str, ...]
    source: str
    highway: str | None = None


def _landmark(entry: object, path: object) -> Landmark:
    ident = entry.get("id", "<unknown>") if isinstance(entry, dict) else "<unknown>"
    try:
        if not isinstance(entry, dict):
            raise ValueError("entry must be a mapping")
        for key in ("id", "name", "state", "kind", "source"):
            if not isinstance(entry[key], str) or not entry[key].strip():
                raise ValueError(f"{key} must be nonempty text")
        numeric_keys = ("lat", "lon", "radius_km")
        if any(isinstance(entry[key], bool) for key in numeric_keys):
            raise ValueError("coordinates/radius must be numeric")
        values = {key: float(entry[key]) for key in numeric_keys}
        if not all(isfinite(v) for v in values.values()):
            raise ValueError("coordinates/radius must be finite")
        if (
            not -90 <= values["lat"] <= 90
            or not -180 <= values["lon"] <= 180
            or values["radius_km"] <= 0
        ):
            raise ValueError("coordinates/radius out of range")
        facts = entry["facts"]
        if not isinstance(facts, list) or not all(isinstance(f, str) and f.strip() for f in facts):
            raise ValueError("facts must be a list of text")
        url = urlparse(entry["source"])
        if url.scheme not in {"http", "https"} or not url.netloc:
            raise ValueError("source must be an HTTP(S) URL")
        highway = entry.get("highway")
        if highway is not None and not isinstance(highway, str):
            raise ValueError("highway must be text")
        return Landmark(
            **{key: entry[key] for key in ("id", "name", "state", "kind", "source")},
            **values,
            facts=tuple(facts),
            highway=highway,
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ConfigError(f"{path}: landmark {ident}: {exc}") from exc


def _load_file(path: Traversable) -> list[tuple[Traversable, Landmark]]:
    try:
        entries = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{path}: landmark <unknown>: {exc}") from exc
    if not isinstance(entries, list):
        raise ConfigError(f"{path}: landmark <unknown>: pack must be a YAML list")
    return [(path, _landmark(entry, path)) for entry in entries]


@dataclass(frozen=True)
class KnowledgePack:
    landmarks: tuple[Landmark, ...] = ()

    @classmethod
    def load(cls, paths: Iterable[Traversable]) -> "KnowledgePack":
        landmarks: list[Landmark] = []
        seen: set[str] = set()
        for path, landmark in chain.from_iterable(_load_file(path) for path in paths):
            if landmark.id in seen:
                raise ConfigError(f"{path}: landmark {landmark.id}: duplicate id")
            seen.add(landmark.id)
            landmarks.append(landmark)
        return cls(tuple(landmarks))

    @classmethod
    def for_game(cls, game: str, extra_paths: list[str] | None = None) -> "KnowledgePack":
        root = files("familiar.games.scs").joinpath(f"knowledge/{game}")
        paths = sorted(
            (p for p in root.iterdir() if p.name.endswith((".yaml", ".yml"))), key=lambda p: p.name
        )
        for extra in extra_paths or []:
            path = Path(extra)
            if path.is_dir():
                paths.extend(sorted(p for p in path.iterdir() if p.suffix in {".yaml", ".yml"}))
                continue
            paths.append(path)
        return cls.load(paths)

    def nearby(self, lat: float, lon: float) -> list[tuple[Landmark, float]]:
        distances = [(p, haversine(lat, lon, p.lat, p.lon)) for p in self.landmarks]
        return sorted(((p, d) for p, d in distances if d <= p.radius_km), key=lambda pair: pair[1])
