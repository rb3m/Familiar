"""Check a landmark knowledge pack against Wikipedia: the article exists and its coordinates
are within a tolerance of ours. Prints one line per landmark and a summary.

Usage: uv run python scripts/verify_landmarks.py PACK.yaml [--max-km 5]
"""

import argparse
import math
import sys
import urllib.parse
from pathlib import Path

import httpx
import yaml

API = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "FamiliarLandmarkCheck/0.1 (personal project)"}


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371 * math.asin(math.sqrt(h))


def title_from(url: str) -> str | None:
    marker = "wikipedia.org/wiki/"
    if marker not in url:
        return None
    return urllib.parse.unquote(url.split(marker, 1)[1]).split("#")[0].replace("_", " ")


def wiki_coordinates(client: httpx.Client, titles: list[str]) -> dict[str, tuple | None]:
    """Resolve redirects; map each requested title to (lat, lon), None (no coords) or 'missing'."""
    params = {
        "action": "query",
        "format": "json",
        "prop": "coordinates",
        "redirects": 1,
        "titles": "|".join(titles),
    }
    data = client.get(API, params=params).json()["query"]
    normalized = {r["from"]: r["to"] for r in data.get("normalized", [])}
    redirects = {r["from"]: r["to"] for r in data.get("redirects", [])}
    pages = {p["title"]: p for p in data["pages"].values()}
    result = {}
    for title in titles:
        final = normalized.get(title, title)
        final = redirects.get(final, final)
        result[title] = _page_coordinates(pages.get(final, {"missing": True}))
    return result


def _page_coordinates(page: dict) -> tuple | str | None:
    if "missing" in page:
        return "missing"
    coords = page.get("coordinates")
    return (coords[0]["lat"], coords[0]["lon"]) if coords else None


def check(entry: dict, found: object, max_km: float) -> str:
    if found == "missing":
        return "NO-ARTICLE"
    if found is None:
        return "NO-COORDS"
    distance = haversine_km((entry["lat"], entry["lon"]), found)
    return "OK" if distance <= max_km else f"OFF {distance:.0f} km"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pack")
    parser.add_argument("--max-km", type=float, default=5.0)
    args = parser.parse_args()
    entries = yaml.safe_load(Path(args.pack).read_text())
    titled = [(e, title_from(e.get("source", ""))) for e in entries]
    results: dict[str, object] = {}
    with httpx.Client(headers=HEADERS, timeout=30) as client:
        titles = sorted({t for _, t in titled if t})
        for start in range(0, len(titles), 40):
            results |= wiki_coordinates(client, titles[start : start + 40])
    tally: dict[str, int] = {}
    for entry, title in titled:
        status = check(entry, results.get(title), args.max_km) if title else "NOT-WIKIPEDIA"
        tally[status.split()[0]] = tally.get(status.split()[0], 0) + 1
        print(f"{status:14} {entry['state']} {entry['id']}")
    print("summary:", tally)
    return 0


if __name__ == "__main__":
    sys.exit(main())
