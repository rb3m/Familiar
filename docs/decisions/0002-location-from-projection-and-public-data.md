# 0002 Location awareness from the game projection and public data

- Status: accepted
- Date: 2026-11-15

## Context

The companion should mention where the truck is ("welcome to Idaho") and nearby landmarks (the
user's example: driving past the world's largest pumpkin). The telemetry server reports only game
world coordinates (`placement.x`, `placement.z`), not latitude/longitude.

## Options

1. Extract city and landmark positions from the game's map files: exact in-game positions, but
   needs archive extraction tooling, breaks with every map update, and the data is SCS's.
2. Key landmarks by game coordinates recorded by hand while driving: exact, but someone must
   drive past every landmark first.
3. Convert game coordinates to WGS84 with the projection the game itself declares
   (`def/climate.sii`: Lambert conformal conic plus scale factors, as documented by the
   truckermudgeon/maps project), then use real-world public data: the US Census Gazetteer for
   towns/states (public domain) and curated landmark packs with real coordinates and sources.

## Decision

Option 3, implemented with our own small LCC inverse (no GPL code copied, no new dependency).
The game map is compressed, so converted positions are typically ~25 km from the real place
(Kaycee, WY: 24 km). That is fine for state and nearest-town awareness. Landmark radii are
generous (15–60 km), and every landmark carries a source URL that is machine-checked against
Wikipedia coordinates. Option 2 stays open as a later "mark this spot" refinement for
landmarks that need precise triggers.

## Who agreed

Coordinator decision (Claude) within ADR 0001's grounding principle; the trade-off (precision vs
maintenance) is low-risk and reversible, so no separate consensus round was run.
