Research task (use web search). Write the result to the file landmarks.yaml in the current directory. Do not create other files.

Goal: a knowledge pack of real-world landmarks for a voice companion in Euro Truck Simulator 2. Countries (ISO codes): {states}.

Choose the most notable landmarks a traveller passing through would recognise or enjoy, most notable first. Mix famous sights, natural wonders, historic sites, engineering feats and quirky roadside things near motorways/main roads. Spread landmarks across each country, not clustered in one city (at most 2 per city). Keep the most notable first in the YAML list, because nearby entries will be dropped in that order during spacing checks.

Requested landmark counts per country:
{counts}

Region notes (apply when listed):
{region_notes}

YAML format, a top-level list, one entry per landmark:
- id: kebab-case-unique
  name: "Official name"
  state: XX          # ISO country code; keep the schema key named state
  lat: 12.3456       # WGS84 decimal degrees from the English Wikipedia article coordinates
  lon: 12.3456
  radius_km: 25      # 15 for small roadside things, up to 60 for big natural features
  kind: roadside | natural | historic | engineering | quirky
  highway: "A1"      # nearest motorway/main road, if known
  facts:             # 2-4 short, verifiable facts, each one sentence
    - "..."
  source: "https://en.wikipedia.org/wiki/..."

Rules: only real places; every coordinate and fact must come from the English Wikipedia source article. Use the coordinates in that article, not a city centre or road position. The article title must exist exactly, because coordinates and facts will be machine-checked. If unsure about something, leave it out. No commentary outside the YAML file.
