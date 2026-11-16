# SCS game data

- `us_places.csv`: US Census Bureau 2025 Gazetteer Files, places (public domain). Active,
  incorporated places in the contiguous US with internal-point coordinates and land area.
  Regenerate with `scripts/build_places.py`.
- `eu_places.csv`: [GeoNames](https://www.geonames.org/) cities5000 data,
  licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
  ETS2 map countries within latitude 34–72 and longitude -11–45, using plain names,
  coordinates and population. `state` holds the ISO country code (including XK for
  Kosovo); land area is unavailable and left empty.
  Download and unzip <https://download.geonames.org/export/dump/cities5000.zip>, then
  regenerate with `uv run python scripts/build_eu_places.py cities5000.txt`.
