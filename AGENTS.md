# Familiar

## What and why
Familiar is a voice companion with an original persona, Mira, that chats with the player
and interjects about what is happening in sim games: landmarks near the truck in ATS, a pirate-
infested system in Elite, a burn in KSP. It also works away from games as a desk assistant on the
Mac. Everything is modular: LLM, speech-to-text, text-to-speech and game adapters are plugins
behind `typing.Protocol` interfaces, chosen in `config.toml`; personas are YAML data. Gemini (free
API tier) is the first backend; others plug in via entry points.

## Current step
- Milestone: **M3 Memory** built 2026-11-16 (ADR 0005, `familiar/memory/`); needs a live check: a second drive past a place from an earlier session, "have we been here before?", and a summary row after quitting. M2 done; ETS2 (M4) live. KSP live (ADR 0006, two orbits 2026-11-17); voice-set alerts via LLM tools (ADR 0007, API 2.0) verified in flight.
- Next task: live-verify M3, then tune from drives: `JAM_*` (`familiar_scs/events.py`), `SOLAR_NOON_HOUR` (`geo.py`, 13:00 unverified), recall gate (`memory/keeper.py`). Unverified: ETS2 UK projection, Iceland, ETS2 auto wipers. Later: kRPC-fed Touch Portal KSP panel.
- Known: LLM is flash-lite on the free key; quota 429s park a model 15 min and fall back to `fallback_api_key_env` (paid, capped). ElevenLabs Pro (library voices need a paid tier); Gemini TTS is the fallback. GUI sessions need the evdev (joystick) push-to-talk. Telemetry has no traffic/weather data; jams and rain are inferred.

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
- `docs/decisions/`: ADRs. Read 0001 for structure, 0002 for location/landmarks, 0003 for GUI, 0004 for package boundaries, 0005 for trip memory, 0006 KSP/flavor quotes, 0007 tools/alerts.
- `docs/gui-api.md`: GUI server/frontend contract (endpoints, auth, SSE). Read before changing `packages/familiar/src/familiar/gui/`.
- `config.example.toml`: every config key with defaults.
- `packages/familiar/src/familiar/personas/`: packaged built-in persona YAML; user personas live in `~/.config/familiar/personas/` and shadow them.
- `packaging/linux/`: menu launcher + icon installer (`install.sh`).
- `packages/familiar/src/familiar/`: core voice loop, backends, Director and GUI; no imports from game packs.
- `packages/familiar-scs/src/familiar_scs/`: ATS/ETS2 adapters, commands, data and `knowledge/<game>/` packs.
- `packages/familiar-ksp/src/familiar_ksp/`: KSP kRPC telemetry, events, quotes and probe/record commands.
- `docs/knowledge-packs.md`: landmark pack format and the `familiar landmarks status|add|check` workflow. Read before adding or editing landmarks.
- `scripts/`: data builders; landmark scripts are thin wrappers around `packages/familiar-scs/src/familiar_scs/curation.py`.
- `packages/*/tests/`: core, SCS and KSP pytest suites.
