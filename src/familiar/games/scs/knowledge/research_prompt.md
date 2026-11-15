Research task (use web search). Write the result to the file landmarks.yaml in the current directory. Do not create other files.

Goal: a knowledge pack of real-world landmarks a truck driver would pass near, for a voice companion in American Truck Simulator. States (USPS codes): {states}.

For EACH state give {per_state} landmarks near major highways/interstates: a mix of quirky roadside attractions ("world's largest X"), natural wonders, historic sites, and engineering feats. Specifically look for any "world's largest pumpkin" or giant-pumpkin roadside attraction in these states and include it if real.

YAML format, a top-level list, one entry per landmark:
- id: kebab-case-unique
  name: "Official name"
  state: XX
  lat: 12.3456        # decimal degrees, taken from the source page (Wikipedia coordinates preferred)
  lon: -123.4567
  radius_km: 25       # 15 for small roadside things on a highway, up to 60 for big natural features
  kind: roadside | natural | historic | engineering | quirky
  highway: "I-80"     # nearest major highway, if known
  facts:              # 2-4 short, verifiable facts, each one sentence
    - "..."
  source: "https://en.wikipedia.org/wiki/..."   # a real page that confirms the facts and coordinates

Rules: only real places; every coordinate and fact must come from the source page; prefer English Wikipedia URLs (they will be machine-checked against the Wikipedia API, so the article title must exist exactly). If unsure about something, leave it out. No commentary outside the YAML file.
