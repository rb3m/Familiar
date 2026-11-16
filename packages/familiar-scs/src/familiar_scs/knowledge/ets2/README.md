# ETS2 landmarks

Curate countries with `familiar landmarks add DE FR --game ets2 --dry-run`, then
omit `--dry-run` to merge into `landmarks.yaml` here. The file is created on the
first merge with a leading comment block. Check it without modification using
`familiar landmarks check --game ets2`. Installed-content `status` is ATS-only.

Research follows `research_prompt.md`: most notable sights first, spread across
each country, at most two per city and near motorways/main roads. Turkey covers
only East Thrace including Istanbul; Russia only Kaliningrad Oblast and the
St Petersburg/Leningrad/Pskov region; Finland mainly southern and central areas.

Default research quotas:

- 20: DE FR ES IT GB PL SE NO
- 12: PT AT CZ RO GR FI HU NL DK BG CH HR RS
- 6: BE SK SI BA EE LV LT AL MK ME RU TR XK
- 3: LU and other supported countries

`--per-state N` overrides all requested countries. After coordinate and fact
checks, entries within 30 km of an existing entry or an earlier survivor are
dropped before staging; the most notable first wins, even across borders. Quotas
are research targets, so checks and spacing may leave fewer landmarks.

Packs are YAML lists. Required keys: id, name, state, lat, lon, radius_km, kind,
facts (list of strings), source (HTTP(S) URL). Optional: highway. The `state` key
holds an ISO country code, including RU for Russia. Coordinates are WGS84 degrees
from the English Wikipedia article, radii are kilometres, and IDs must be unique
across packs. See `docs/knowledge-packs.md` for reports and review guidance.
