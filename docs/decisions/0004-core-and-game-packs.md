# 0004 Core and game packs in a uv workspace

- Status: proposed
- Date: 2026-11-15

## Context

The voice loop, backends, Director, personas and GUI are useful without ATS or ETS2. Installing
core should not install SCS telemetry code or landmark data. Game-specific CLI commands and GUI
metadata must remain discoverable without coupling core to a particular game. The shipped example
persona must be original; Mira is packaged with core rather than loaded from a repository path.

## Options

- A: A uv workspace with independently installable `familiar` and `familiar-scs` distributions.
  Shared development and tests remain in one repository; game code and data ship separately.
- B: Separate repositories. Strong separation, but cross-package changes, releases and development
  require more coordination.
- C: Extras only. Convenient dependency selection, but extras do not remove game code and data
  from the core wheel, so this does not meet the installation boundary.

## Decision

Choose A. The root is a virtual workspace; packages live under `packages/`. Core owns the voice
loop and GUI and has no imports from `familiar_scs`. The SCS distribution owns ATS/ETS2 adapters,
telemetry tools, landmark commands and resources. `familiar.games` exposes adapters and their
class-level display metadata. The new `familiar.commands` protocol exposes CLI commands with the
same plugin API major-version check as other entry-point groups. Broken command plugins warn
and are skipped so they cannot disable core CLI commands.

`familiar-scs` depends on `familiar>=0.1.0,<1.0.0`, resolved from the workspace during development.
A subprocess test imports every core module without game entry points and checks that no SCS
module was loaded. CLI and GUI tests cover an empty game-plugin list. Wheel contents are checked
to ensure SCS data stays in the SCS distribution. Core ships the original Mira persona as package
data; user personas override optional configured personas, which override the packaged defaults.

## Who agreed

- Claude: proposed option A; personas of existing characters move out of the repo (the
  Commander's request), the core ships the original persona Mira.
- agy: option A. Top risk: `familiar.commands` / adapter contract drift between packages after
  release, so command plugins share the PLUGIN_API_VERSION check and familiar-scs pins
  `familiar<1.0`.
- codex (gpt-6.1-sol, medium): option A. Top risk: accidental core→game coupling, so a core test
  asserts no `familiar_scs` import and the core wheel was smoke-tested on its own.
