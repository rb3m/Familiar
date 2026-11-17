# Familiar

## What and why
Familiar is a voice companion with an original persona, Mira, that chats with the player
and interjects about what is happening in sim games: landmarks near the truck in ATS, a pirate-
infested system in Elite, a burn in KSP. It also works away from games as a desk assistant on the
Mac. Everything is modular: LLM, speech-to-text, text-to-speech and game adapters are plugins
behind `typing.Protocol` interfaces, chosen in `config.toml`; personas are YAML data. Gemini (free
API tier) is the first backend; others plug in via entry points.

## Current step
- Milestone: **M5 Hands** built 2026-11-17 (ADR 0009, `familiar/hands.py`, `keyboard/uinput.py`, SCS `bindings.py`, KSP `actions.py`); gate verified live with flash-lite in en/es/ja. Needs in-game checks: SCS key presses land (hold times, toggles, state read-back), kRPC actions in flight. M3 Memory still needs its live check (second drive past a known place, "have we been here before?", summary row).
- Next task: live-verify M5 and M3, then tune `JAM_*` (`familiar_scs/events.py`), `SOLAR_NOON_HOUR` (`geo.py`), recall gate (`memory/keeper.py`). Then Touch Portal sync (both ways) and a KWin window-focus check for key presses (roadmap).
- Known: LLM is flash-lite on the free key; quota 429s park a model 15 min and fall back to `fallback_api_key_env`. ElevenLabs Pro; Gemini TTS fallback. GUI sessions need evdev push-to-talk. Key presses go to whatever window has focus (guard: fresh, unpaused telemetry only). Radio power is unbound in SCS by default.

## Rules
- Python 3.12 via uv: `uv sync --all-packages`, `uv run familiar …`. PEP-8; lint `uv run ruff check .`, format `uv run ruff format .`.
- Tests: `uv run pytest` (no network; live API tests marked `live`, run with `-m live`).
- Prefer early returns and mappings over nested ifs and `else`.
- Plugins depend only on `familiar.interfaces`; bump `PLUGIN_API_VERSION` only for breaking changes.
- Secrets only via env vars; never commit `config.toml` or keys.
- Don't hardcode what we don't control (game data, mods, bindings, model names): `*/data/*.yaml` via `load_data`, overrides in `~/.config/familiar/data/`.
- Internal dates ISO 8601; SI units in telemetry and knowledge packs.
- One ADR per real decision in `docs/decisions/NNNN-slug.md`; ordinary "why" goes in commit messages.

## Index
- `README.md`: user-facing overview and install. Keep it in step with features.
- `docs/architecture.md`: modules, interfaces, Director rules, platforms. Read before changing interfaces or adding a module.
- `docs/plugins.md`: how to add a backend, command, or game pack. Read before writing a plugin.
- `docs/roadmap.md`: milestones and done-criteria. Read when picking the next task.
- `docs/decisions/`: ADRs. Read 0001 for structure, 0002 for location/landmarks, 0003 for GUI, 0004 for package boundaries, 0005 for trip memory, 0006 KSP/flavor quotes, 0007 tools/alerts, 0008 API vs subscription CLI (latency), 0009 hands/confirmation gate.
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
