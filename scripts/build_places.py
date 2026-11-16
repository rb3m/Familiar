"""Regenerate the SCS pack gazetteer from the US Census Gazetteer.

Usage: download https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/
2025_Gaz_place_national.zip, unzip it, then run:
    uv run python scripts/build_places.py 2025_Gaz_place_national.txt
Keeps active, incorporated places (no CDPs) in the contiguous US. Public domain data.
"""

import csv
import re
import sys
from pathlib import Path

OUT = Path(__file__).parent.parent / "packages/familiar-scs/src/familiar_scs/data/us_places.csv"
SKIP_STATES = {"AK", "HI", "PR"}
CDP = "57"
SUFFIX = re.compile(
    r"\s+(city|town|village|borough|municipality|city and borough|metropolitan government.*"
    r"|consolidated government.*|unified government.*|urban county|corporation)"
    r"(\s*\(balance\))?$",
    re.I,
)


def keep(row: dict) -> bool:
    return row["USPS"] not in SKIP_STATES and row["LSAD"] != CDP and row["FUNCSTAT"] == "A"


def convert(row: dict) -> tuple:
    return (
        SUFFIX.sub("", row["NAME"]),
        row["USPS"],
        round(float(row["INTPTLAT"]), 4),
        round(float(row["INTPTLONG"]), 4),
        round(int(row["ALAND"]) / 1e6, 1),
    )


def main(source: str) -> None:
    with open(source, encoding="latin-1") as handle:
        rows = [
            {k.strip(): v.strip() for k, v in r.items()}
            for r in csv.DictReader(handle, delimiter="|")
        ]
    places = sorted((convert(r) for r in rows if keep(r)), key=lambda r: (r[1], r[0]))
    with OUT.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name", "state", "lat", "lon", "land_km2"])
        writer.writerows(places)
    print(f"{len(places)} places -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1])
