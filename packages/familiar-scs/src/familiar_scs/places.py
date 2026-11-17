"""Cached, vectorised nearest-place lookup from the packaged gazetteer."""

import csv
from dataclasses import dataclass, replace
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import TextIO

import numpy as np
from familiar.interfaces import ConfigError

from .data_files import mapping_data, require, string_mapping
from .geo import EARTH_RADIUS_KM


def load_regions() -> dict[str, dict[str, str]]:
    data, source = mapping_data("regions")
    require(set(data) == {"ats", "ets2"}, source, "expected ats and ets2 regions")
    require(
        all(string_mapping(regions) for regions in data.values()),
        source,
        "regions must map codes to names",
    )
    return data


REGION_NAMES = load_regions()
STATE_NAMES = REGION_NAMES["ats"]
COUNTRY_NAMES = REGION_NAMES["ets2"]


@dataclass(frozen=True)
class Place:
    name: str
    state: str
    lat: float
    lon: float
    land_km2: float = 0.0
    distance_km: float = 0.0
    population: int = 0


@lru_cache(maxsize=2)
def _packaged_places(filename: str = "us_places.csv") -> tuple[Place, ...]:
    resource = files("familiar_scs").joinpath(f"data/{filename}")
    if not resource.is_file():
        raise ConfigError(f"Missing SCS gazetteer: {resource}")
    with resource.open("r", encoding="utf-8", newline="") as stream:
        return _read_places(stream)


def _read_places(stream: TextIO) -> tuple[Place, ...]:
    return tuple(
        Place(
            row["name"],
            row["state"],
            float(row["lat"]),
            float(row["lon"]),
            float(row.get("land_km2") or 0),
            population=int(row.get("population") or 0),
        )
        for row in csv.DictReader(stream)
    )


class PlaceIndex:
    def __init__(
        self, path: str | Path | None = None, *, places: tuple[Place, ...] | None = None
    ) -> None:
        self.places = self._load(path, places)
        self._coords = np.radians([(p.lat, p.lon) for p in self.places]).reshape(-1, 2)

    @staticmethod
    def _load(path: str | Path | None, places: tuple[Place, ...] | None) -> tuple[Place, ...]:
        if places is not None:
            return places
        if path is None:
            return _packaged_places()
        with Path(path).open(encoding="utf-8", newline="") as stream:
            return _read_places(stream)

    def nearest(self, lat: float, lon: float) -> Place | None:
        if not self.places:
            return None
        a, b = np.radians([lat, lon])
        h = (
            np.sin((self._coords[:, 0] - a) / 2) ** 2
            + np.cos(a) * np.cos(self._coords[:, 0]) * np.sin((self._coords[:, 1] - b) / 2) ** 2
        )
        idx = int(np.argmin(h))
        distance = 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(h[idx], 0, 1)))
        return replace(self.places[idx], distance_km=float(distance))
