"""Regenerate the ETS2 gazetteer from GeoNames cities5000.txt.

Download https://download.geonames.org/export/dump/cities5000.zip, unzip it, then run:
    uv run python scripts/build_eu_places.py cities5000.txt
Keeps ETS2 map countries within European bounds. GeoNames data is CC BY 4.0.
"""

import csv
import sys
from pathlib import Path

from familiar_scs.places import COUNTRY_NAMES

OUT = Path(__file__).parent.parent / "packages/familiar-scs/src/familiar_scs/data/eu_places.csv"
COUNTRIES = COUNTRY_NAMES.keys()
FIELDS = [
    "geonameid",
    "name",
    "asciiname",
    "alternatenames",
    "lat",
    "lon",
    "feature_class",
    "feature_code",
    "country_code",
    "cc2",
    "admin1",
    "admin2",
    "admin3",
    "admin4",
    "population",
    "elevation",
    "dem",
    "timezone",
    "moddate",
]


def keep(row: dict) -> bool:
    return (
        row["country_code"] in COUNTRIES
        and 34 <= float(row["lat"]) <= 72
        and -11 <= float(row["lon"]) <= 45
        and (row["country_code"] != "RU" or float(row["lon"]) <= 35)
    )


def convert(row: dict) -> tuple:
    return (
        row["name"],
        row["country_code"],
        round(float(row["lat"]), 4),
        round(float(row["lon"]), 4),
        "",
        int(row["population"] or 0),
    )


def main(source: str) -> None:
    with open(source, encoding="utf-8", newline="") as handle:
        rows = csv.DictReader(handle, fieldnames=FIELDS, delimiter="\t")
        places = sorted((convert(r) for r in rows if keep(r)), key=lambda r: (r[1], r[0]))
    with OUT.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name", "state", "lat", "lon", "land_km2", "population"])
        writer.writerows(places)
    print(f"{len(places)} places -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1])
