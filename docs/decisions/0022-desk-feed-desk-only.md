# 0022: The desk feed runs only in desk sessions (amends 0016)

Date: 2026-11-22
Status: Accepted

## Context

ADR 0016 ran the `desk` service at a bare desk and beside real games, so a late-night remark
could still come up mid-drive. With weather (ADR 0019) the feed now carries the Commander's real
local conditions. Games often have their own time and weather, and a planned MSFS pack will
report the weather at the airport the player is landing at; real Corregidora weather in that
prompt would be wrong and confusing.

## Options

1. Keep the feed beside games (0016 as is).
2. Drop only the weather source in game sessions, keep the real clock.
3. Run the whole desk service only when there is no game adapter.

## Decision

Option 3. `Runtime.conversation()` starts the `desk` service only when no game is selected.
Desk sessions are unchanged, including a game without telemetry played in desk mode (the Mac
case 0016 was written for). In game sessions the game supplies time and weather, or nothing does.
`[desk] enabled = true` is safe to leave on for both kinds of session.

## Consequences

- No more "it's getting late" remarks from the real clock during a game session; a game pack
  that wants that can describe its own time.
- No config key: nothing asked for real local context inside a game. If one is wanted later,
  it goes in a separate setting rather than reverting this.

## Who agreed

- Commander: real weather desk-only; chose dropping the whole feed (clock and weather) in games.
- Claude (coordinator): recommended option 3 for one simple rule; Codex implemented it.
