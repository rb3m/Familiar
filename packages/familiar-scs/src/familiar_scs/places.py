"""Cached, vectorised nearest-place lookup from the packaged gazetteer."""

import csv
from dataclasses import dataclass, replace
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import TextIO

import numpy as np
from familiar.interfaces import ConfigError

from .geo import EARTH_RADIUS_KM

STATE_NAMES = dict(
    AL="Alabama",
    AZ="Arizona",
    AR="Arkansas",
    CA="California",
    CO="Colorado",
    CT="Connecticut",
    DE="Delaware",
    FL="Florida",
    GA="Georgia",
    ID="Idaho",
    IL="Illinois",
    IN="Indiana",
    IA="Iowa",
    KS="Kansas",
    KY="Kentucky",
    LA="Louisiana",
    ME="Maine",
    MD="Maryland",
    MA="Massachusetts",
    MI="Michigan",
    MN="Minnesota",
    MS="Mississippi",
    MO="Missouri",
    MT="Montana",
    NE="Nebraska",
    NV="Nevada",
    NH="New Hampshire",
    NJ="New Jersey",
    NM="New Mexico",
    NY="New York",
    NC="North Carolina",
    ND="North Dakota",
    OH="Ohio",
    OK="Oklahoma",
    OR="Oregon",
    PA="Pennsylvania",
    RI="Rhode Island",
    SC="South Carolina",
    SD="South Dakota",
    TN="Tennessee",
    TX="Texas",
    UT="Utah",
    VT="Vermont",
    VA="Virginia",
    WA="Washington",
    WV="West Virginia",
    WI="Wisconsin",
    WY="Wyoming",
    DC="District of Columbia",
)


COUNTRY_NAMES = dict(
    AT="Austria",
    BE="Belgium",
    BA="Bosnia and Herzegovina",
    BG="Bulgaria",
    HR="Croatia",
    CZ="Czechia",
    DK="Denmark",
    EE="Estonia",
    FI="Finland",
    FR="France",
    DE="Germany",
    GR="Greece",
    HU="Hungary",
    IE="Ireland",
    IT="Italy",
    LV="Latvia",
    LT="Lithuania",
    LU="Luxembourg",
    MK="North Macedonia",
    ME="Montenegro",
    NL="Netherlands",
    NO="Norway",
    PL="Poland",
    PT="Portugal",
    RO="Romania",
    RS="Serbia",
    RU="Russia",
    SK="Slovakia",
    SI="Slovenia",
    ES="Spain",
    SE="Sweden",
    CH="Switzerland",
    TR="Turkey",
    GB="United Kingdom",
    AL="Albania",
    AD="Andorra",
    LI="Liechtenstein",
    MC="Monaco",
    SM="San Marino",
    VA="Vatican City",
    MT="Malta",
    XK="Kosovo",
)
REGION_NAMES = {"ats": STATE_NAMES, "ets2": COUNTRY_NAMES}


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
