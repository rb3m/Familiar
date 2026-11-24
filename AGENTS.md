# Familiar

## What and why
Familiar is a voice companion with an original persona, Mira, that chats with the player
and interjects about what is happening in sim games: landmarks near the truck in ATS, a pirate-
infested system in Elite, a burn in KSP. It also works away from games as a desk assistant on the
Mac. Everything is modular: LLM, speech-to-text, text-to-speech and game adapters are plugins
behind `typing.Protocol` interfaces, chosen in `config.toml`; personas are YAML data. Gemini (free
API tier) is the first backend; others plug in via entry points.

## Current step
- Live OK: KSP, ETS2 recall/stereo, Elite, DJ blend, Mac desk, M1 voice server (ADR 0029; Mac app via SSH tunnel, ADR 0030). Awaiting live: local voice in a full session, scene topics (ADR 0026), research, topics, system health, desk weather, desk-feed remarks, controls/DJ split + guest picks, **M5** SCS keys, callbacks, Elite hand-off.
- Next: live checks above; local voice on Linux; tune `JAM_*` (`familiar_scs/events.py`), `SOLAR_NOON_HOUR` (`geo.py`), recall gate (`memory/keeper.py`); Touch Portal, KWin focus; M7 Vic2 adapter.
- Known: 429s park a model 15 min, then `fallback_api_key_env`. GUI needs non-terminal PTT (joystick, mouse button, F-key or keypad). Key presses go to the focused window (guard: fresh, unpaused telemetry).

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
- `docs/decisions/`: one ADR per decision (`NNNN-slug.md`); read the matching one before changing that area.
- `docs/gui-api.md`: GUI contract (endpoints, auth, SSE). Read before changing `familiar/gui/`.
- `config.example.toml`: every config key with defaults.
- `voice-server/`: M1 LAN TTS server, own uv project (`cd voice-server && uv run pytest`). Read `docs/voice-server.md` before changing it or `backends/local/`.
- `packages/familiar/src/familiar/personas/`: built-in persona YAML; user copies in `~/.config/familiar/personas/` shadow them.
- `packaging/linux/`, `packaging/macos/`: launchers and installers (menu entry; `Familiar.app`).
- `packages/familiar/src/familiar/`: core voice loop, backends, Director and GUI; no imports from game packs.
- `packages/familiar-scs/`: ATS/ETS2 adapters, commands, data and `knowledge/<game>/` packs.
- `packages/familiar-ksp/`: KSP kRPC telemetry, events, quotes and probe/record commands.
- `packages/familiar-elite/`: Elite journal/status, events, `.binds` writer (`elite-bindings`), hands.
- `packages/familiar-desk/`: `desk-feed` helper, source and weather-provider plugins, `desk` service.
- `packages/familiar-research/`: `research` service (agy lookups), `research check`.
- `packages/familiar-system/`: `system` service (health lookups, alerts), `system sensors`.
- `packages/familiar-music/`: DJ service: Pear + mpv players, sources, `library/` index, `music` CLI.
- `docs/knowledge-packs.md`: landmark packs and `familiar landmarks`. Read before editing landmarks.
- `scripts/`: data builders; landmark scripts wrap `familiar_scs/curation.py`.
- `packages/*/tests/`: per-package pytest suites.
