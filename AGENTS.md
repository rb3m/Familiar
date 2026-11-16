# Familiar

## What and why
Familiar is a voice companion with an original persona, Mira, that chats with the player
and interjects about what is happening in sim games: landmarks near the truck in ATS, a pirate-
infested system in Elite, a burn in KSP. It also works away from games as a desk assistant on the
Mac. Everything is modular: LLM, speech-to-text, text-to-speech and game adapters are plugins
behind `typing.Protocol` interfaces, chosen in `config.toml`; personas are YAML data. Gemini (free
API tier) is the first backend; others plug in via entry points.

## Current step
- Milestone: **M2 ATS**: first live drive from the GUI 2026-11-15; speech used miles correctly. Core and game packs split into two packages (ADR 0004).
- Next task: fix barge-in bug: after the user talks over a reply, audio stays silent while text replies continue (transcript of 2026-11-16 00:47 UTC). Then confirm heading, tune Director cooldowns, check ETS2 km.
- Known: LLM default is flash-lite (free-tier 3.8-flash stalls). ElevenLabs free tier 10k chars/month, then Gemini TTS. GUI sessions need the evdev (joystick) push-to-talk.

## Rules
- Python 3.12 via uv: `uv sync --all-packages`, `uv run familiar …`. PEP-8; lint `uv run ruff check .`, format `uv run ruff format .`.
- Tests: `uv run pytest` (no network; live API tests marked `live`, run with `-m live`).
- Prefer early returns and mappings over nested ifs and `else`.
- Plugins depend only on `familiar.interfaces`; bump `PLUGIN_API_VERSION` only for breaking changes.
- Secrets only via env vars; never commit `config.toml` or keys.
- Internal dates ISO 8601; SI units in telemetry and knowledge packs.
- One ADR per real decision in `docs/decisions/NNNN-slug.md`; ordinary "why" goes in commit messages.

## Index
- `README.md`: user-facing overview and install. Keep it in step with features.
- `docs/architecture.md`: modules, interfaces, Director rules, platforms. Read before changing interfaces or adding a module.
- `docs/plugins.md`: how to add a backend, command, or game pack. Read before writing a plugin.
- `docs/roadmap.md`: milestones and done-criteria. Read when picking the next task.
- `docs/decisions/`: ADRs. Read 0001 before changing the overall structure, 0002 before changing location/landmarks, 0003 before changing the GUI approach, 0004 before changing package boundaries.
- `docs/gui-api.md`: GUI server/frontend contract (endpoints, auth, SSE). Read before changing `packages/familiar/src/familiar/gui/`.
- `config.example.toml`: every config key with defaults.
- `packages/familiar/src/familiar/personas/`: packaged built-in persona YAML; user personas live in `~/.config/familiar/personas/` and shadow them.
- `packaging/linux/`: menu launcher + icon installer (`install.sh`).
- `packages/familiar/src/familiar/`: core voice loop, backends, Director and GUI; no imports from game packs.
- `packages/familiar-scs/src/familiar_scs/`: ATS/ETS2 adapters, commands, data and `knowledge/<game>/` packs.
- `docs/knowledge-packs.md`: landmark pack format and the `familiar landmarks status|add|check` workflow. Read before adding or editing landmarks.
- `scripts/`: data builders; landmark scripts are thin wrappers around `packages/familiar-scs/src/familiar_scs/curation.py`.
- `packages/*/tests/`: core and SCS pytest suites.
