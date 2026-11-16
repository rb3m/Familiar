# Landmark knowledge packs

A pack is a UTF-8 YAML list. Each entry has a unique kebab-case `id`, `name`,
`state` (USPS state code for ATS, ISO country code for ETS2), WGS84 decimal-degree
`lat` and `lon`, positive `radius_km`, `kind`
(roadside, natural, historic, engineering or quirky), a list of short `facts`, and
an HTTP(S) `source` URL. `highway` is optional. Prefer English Wikipedia sources
because the automated checks use its article coordinates and text.

```yaml
- id: example-landmark
  name: Example Landmark
  state: TX
  lat: 30.0
  lon: -100.0
  radius_km: 25
  kind: historic
  highway: I-10
  facts:
  - A short fact supported by the source.
  source: https://en.wikipedia.org/wiki/Example
```

## Workflow

Run `familiar landmarks status` (ATS only) to compare installed ATS state DLC
archives with the bundled pack. California is included with the base game; legacy Nevada and
Arizona archive names are recognized. Non-state DLCs are ignored. A covered state
means it has at least one entry, not that its research is complete.

Set `[games.ats] game_dir` in the Familiar config, or pass `--game-dir PATH` to
status. The fallback is
`~/.local/share/Steam/steamapps/common/American Truck Simulator`.

For a new state, run:

```sh
familiar landmarks add XX YY --per-state 6 --dry-run
familiar landmarks add XX YY --per-state 6
familiar landmarks check path/to/landmarks.yaml
```

For ETS2 countries, select `--game ets2` (the default is `ats`):

```sh
familiar landmarks add DE FR RU --game ets2 --dry-run
familiar landmarks add DE FR RU --game ets2
familiar landmarks check --game ets2
```

The default research quotas per country are:

| Landmarks | Country codes |
| --- | --- |
| 20 | DE FR ES IT GB PL SE NO |
| 12 | PT AT CZ RO GR FI HU NL DK BG CH HR RS |
| 6 | BE SK SI BA EE LV LT AL MK ME RU TR XK |
| 3 | LU and other supported countries |

`--per-state N` overrides the quota for every requested region, including countries.
ATS still defaults to 6 per state. Research uses the game's `research_prompt.md`
in `knowledge/<game>/`. ETS2 asks for the most notable sights first, spread across
each country with at most two per city, near motorways/main roads. Turkey is
limited to European Turkey (East Thrace including Istanbul); Russia to Kaliningrad
Oblast and the St Petersburg/Leningrad/Pskov region; Finland mainly to southern
and central Finland. Codes are validated against the selected game's regions.

`add` runs the `agy` research executable in a fresh work directory (30-minute
limit), checks coordinates, excludes entries that fail, checks facts with the
configured LLM at temperature zero, and stages `landmarks.checked.yaml`. The
summary lists kept/dropped landmarks and changed facts per state. Unless
`--dry-run` is supplied, it merges the survivors into `knowledge/<game>/landmarks.yaml`.
The ETS2 file is created on the first merge with a leading comment block.
`--pack PATH` selects a different destination. Existing IDs and matching
(name, state) pairs are skipped, including duplicates in the new batch. The
pack's leading comment block is preserved.

For ETS2, after the coordinate and fact checks, entries less than 30 km from an
existing pack entry or an earlier survivor are dropped before staging. Research
order is retained, so the most notable nearby sight wins. Spacing applies across
country borders and the summary names the entries dropped for spacing. ATS has
no minimum spacing filter. Quotas are research targets; checks, spacing and
merge deduplication can reduce the final count.

Research needs `agy` on PATH, network access, and working LLM credentials in the
Familiar configuration/environment. `--dry-run` still runs research and both
checks; it only skips the merge. `--workdir DIR` selects the staging directory;
it must not already contain `landmarks.yaml`, to avoid consuming stale output.
The default is `data/landmarks/<UTC timestamp>/` (git-ignored).

`check` defaults to the bundled pack for `--game` (ATS by default) and writes a
JSON fact report in a new work directory without changing the pack. It reports coordinate failures but
still checks every landmark's facts. Both commands print the report path.

The original scripts remain available with their original arguments:

```sh
python scripts/verify_landmarks.py PACK.yaml --max-km 5
python scripts/factcheck_landmarks.py PACK.yaml REPORT.json --delay 4
python scripts/apply_factcheck.py PACK.yaml REPORT.json OUT.yaml
```

The fact-check report is saved after each landmark. Rerunning the fact-check
script with the same report resumes by ID, including previously recorded errors.
To retry an error, remove that item's report entry. Use a fresh report when facts
or sources change: an existing ID is considered done.

## What the checks mean

The coordinate check resolves normalized titles and redirects and compares real
coordinates with Wikipedia within 5 km. It reports missing articles, missing
coordinates, non-Wikipedia sources and positions outside tolerance. This does not
prove that the landmark is visible or reachable on the game's road network.

The fact check asks an LLM to judge facts against the article text. Supported
facts survive; claims and unsupported facts receive supported replacement wording
or are dropped. Checker commentary is stripped. An unavailable article, failed
LLM response or landmark with no surviving facts is excluded from the staged
pack. Wikipedia requests retry with backoff; individual failures appear in the
report. An LLM judgment is fallible, the extract is limited to 30,000 characters,
and neither the article nor the generated corrections are independent proof.
Review the staged pack and reports before relying on new entries.

ATS compresses the map: projected game locations can be roughly 25 km from the
real place. Use generous radii: about 15 km for small roadside attractions,
25 km for typical landmarks, up to 60 km for large natural features. These are
proximity cues, not precise on-screen sightings. Live driving should inform
radius adjustments.

## Future games

Owned-content detection is currently ATS-specific and lives in
`curation.owned_states`. A future game should supply its own installed-content
reader and content identifiers, then connect those to its pack coverage and CLI
options. ETS2 curation already uses country codes, quotas and spacing; installed
content detection remains ATS-only. Coordinate checking, fact reports, cleaning
and merge logic can be
reused when the new game's sources and schema are compatible. Do not assume
that another game's archive names, state codes or map compression match ATS.
