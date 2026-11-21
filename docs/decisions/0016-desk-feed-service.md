# 0016: Desk feed helper and desk companion service

Date: 2026-11-20. Status: accepted.

## Context
The Mac is used at the desk and for games without telemetry, with no controllers. The desk
assistant needs something to talk about besides the user's questions; first the time of day,
later RSS, email and similar. The Commander asked for a helper that samples its inputs at
intervals and writes a JSON file "as if it were telemetry for a game", which Familiar starts.

## Options
1. A `familiar-desk` `GameAdapter` (`game = "desk"`), picked as "Desk" in the game list.
2. A `familiar-desk` `CompanionService` reading the same file.
3. Keep everything in process, with no helper or file.

## Decision
Option 2. The service runs at a bare desk (where core already builds a desk Director for
services) and beside real games, where a late-night remark still makes sense. Option 3 drops the
file the Commander asked for and makes future sources (slow, failure-prone) part of the voice
process.

- `familiar desk-feed` (a `familiar.commands` plugin) is one process with pluggable sources.
  Each source has its own interval and contributes a namespaced payload plus events. A failing
  source records its error and doesn't stop the others.
- File: `<runtime>/familiar/desk-feed.json`, where runtime is `XDG_RUNTIME_DIR` or else the
  per-user temporary directory (macOS sets none). Directory 0700, file 0600, written atomically
  (same-directory temp file and rename). A lock file allows a single writer.
- Envelope: `schema_version`, `instance_id`, `sequence`, `generated_at`, `sources` (each with
  `observed_at`, `expires_at`, `status`, `error`, `data`) and a bounded `events` list. Events have
  stable ids and timestamps, so a reader polling the snapshot never repeats a remark.
- The `desk` service starts the helper when no live writer holds the lock, and stops it on close.
- The first source is `clock`: local time, weekday, day period, and sunrise, sunset and sun
  elevation computed offline from `[desk]` latitude/longitude, with clock periods when unset.

## Agreement
Codex and agy both recommended option 2, the envelope with stable event ids and per-source
cadences, and the runtime-dir file location (agy suggested platformdirs; we keep the existing
`XDG_RUNTIME_DIR`/temp-dir rule already used for the mpv socket).
