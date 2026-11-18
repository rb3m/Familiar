# 0010: Elite Dangerous pack: local journal, written key bindings, cautious EDSM

Date: 2026-11-17
Status: Accepted

## Context

M4 adds Elite Dangerous (Odyssey, 4.4, Steam/Proton app 359320). The Commander flies Solo with a
HOTAS One; flight controls are bound only on the stick, so key presses need keyboard Secondary
bindings. First hands: landing gear, ship lights, cargo scoop, night vision, request docking,
balance/shields power; maybe autodock later. No EDSM account and no uploads. Personas: Mira is
the ship's AI, Frieren the copilot, Megumin a stowaway nobody can explain.

## Options

1. Telemetry: tail the local journal and `Status.json` vs depend on EDMC or EDDN.
2. Bindings: ask the player to bind keys vs Familiar writes them into the `.binds` file.
3. Docking: no dedicated binding exists (`OrderRequestDock` is the fighter order), so either skip
   it or drive the left-panel Contacts tab with a key sequence.
4. "Balance shields": `ResetPowerDistribution` (2/2/2) vs pips to SYS.
5. Exploration hints: live EDSM/Spansh queries vs offline heuristics.

## Decision

- **Telemetry is local only.** Poll the newest `Journal.*.log` (buffer partial lines, follow new
  files, skip the BOM, tolerate unknown fields and events) and the snapshot files `Status.json`,
  `NavRoute.json`, `Cargo.json`. A torn or empty snapshot is "no sample", never a state change;
  keep the last valid one. Read both `Flags` and `Flags2` (on foot, in SRV, in main ship). Paths:
  found under the Proton prefix in every Steam library, with a config override.
- **Familiar writes keyboard Secondaries** (`familiar elite bindings`), only while the game is not
  running: resolve the active preset from `StartPreset.*.start`, back up outside the bindings
  folder, fill only Secondary slots whose device is `{NoDevice}`, never touch Primaries, keep
  preset name and version, write atomically, and record the keys we own in a sidecar file.
  `StartPreset.4.start` holds one preset per category, in the order General, Ship, SRV,
  On-foot (verified on the Commander's files: every control changed in `Custom` is a ship
  control). A category may use a read-only built-in preset from the game's `ControlSchemes`
  folder; Familiar reads it for keys in use and for key presses, never writes it, and tells the
  player to switch that category to Custom when a wanted control lives there.
  Candidate keys are letters, digits and F-keys free across all bindings (no punctuation, dead
  keys, AltGr; numpad only after a test), listed in a data file. A hash check at session start
  warns when the game or the player has changed the file.
- **Toggles are confirmed** from `Status.json` flags (gear, lights, cargo scoop, night vision):
  read, press, check; "already" when the state already matches. Act only in the main ship (or on
  foot for suit night vision), with `GuiFocus == 0` and fresh status.
- **Docking is a best-effort macro**, data-driven, marked experimental: the key sequence lives in
  a data file, the result is read from `DockingRequested`/`DockingGranted`/`DockingDenied`
  (reason spoken), and a denial or timeout is never retried.
- **Power:** "balance power" = `ResetPowerDistribution`; "shields priority" = reset then SYS
  twice (4/1/1), checked against `Pips` (half-pips) in `Status.json`.
- **Exploration hints are "least reported", never "unexplored".** Offline first: distance from
  the bubble, off the Sol–Colonia and Sol–Sagittarius A* corridors, height off the plane. EDSM
  (public, keyless) only on request, one request at a time, cached on disk, named User-Agent,
  backing off on 429. On arrival, the player's own `Scan` flags (`WasDiscovered`,
  `WasMapped`) are the real signal of first discoveries.

## Consequences

Familiar works without EDMC or anything on the Windows side. Editing the binds file means the
game must be restarted to see new keys, and a later edit in the game's controls menu can drop
ours (the hash check catches it). Docking can pick the wrong contact; it is only used when asked
and reports what the journal says. Key names under Proton and the Latin American layout need an
in-game check.

## Who agreed

- codex: local journal, no EDMC/EDDN; no docking binding, Contacts macro is fragile, confirm via
  journal and never repeat; "balance" = reset, offered "power to shields" separately; resolve
  preset, back up, fill empty Secondaries only, atomic write, conflict scan; letters/F-keys,
  test numpad; "promising directions, never guaranteed"; cache, serialise, respect 429.
- agy: direct tailing; partial-write retries; no docking binding, macro fragile; preferred SYS
  pips for "shields" (adopted as a separate action); game closed, backup; avoid punctuation;
  offline heuristics before live sphere queries; check `Flags2` so ship actions never fire on foot.
- grok-4.7 (Copilot): resolve preset from `StartPreset.start`, fill only `{NoDevice}`, hash the
  file, sidecar of owned keys; `Pips` are half-pips; torn snapshots are no sample; wait for
  `DockingRequested`/`Granted`/`Denied`, no retry; "balance" = reset; "least reported" wording;
  EDSM named User-Agent, under ~1 req/s, location sent only when asked.
- Claude (coordinator): merged; balance vs SYS split into two actions since the Commander's phrase
  was ambiguous.
- Commander: chose the six actions, Familiar writes the binds, Solo play, no EDSM uploads,
  persona roles.
