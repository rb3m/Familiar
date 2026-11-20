# Familiar

## What and why
Familiar is a voice companion with an original persona, Mira, that chats with the player
and interjects about what is happening in sim games: landmarks near the truck in ATS, a pirate-
infested system in Elite, a burn in KSP. It also works away from games as a desk assistant on the
Mac. Everything is modular: LLM, speech-to-text, text-to-speech and game adapters are plugins
behind `typing.Protocol` interfaces, chosen in `config.toml`; personas are YAML data. Gemini (free
API tier) is the first backend; others plug in via entry points.

## Current step
- Live OK: KSP crew, banter, kRPC; ETS2 **M3** recall, **M6** stereo; Elite gear, scoop, EDSM; DJ phase 1. Awaiting: DJ library blend (ADR 0015), **M5** SCS key presses, callbacks, Elite hand-off (ADR 0013) + ship/system awareness + `system_info`.
- Next task: live checks above; then tune `JAM_*` (`familiar_scs/events.py`), `SOLAR_NOON_HOUR` (`geo.py`), recall gate (`memory/keeper.py`); Touch Portal sync and KWin focus check; M7 Vic2 save-diff adapter (roadmap).
- Known: flash-lite free key; 429s park a model 15 min, then `fallback_api_key_env`. ElevenLabs Pro, Gemini TTS fallback. GUI needs evdev push-to-talk. Key presses go to the focused window (guard: fresh, unpaused telemetry).

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
- `docs/decisions/`: ADRs. 0001 structure, 0002 location/landmarks, 0003 GUI, 0004 package boundaries, 0005 trip memory, 0006 KSP/quotes, 0007 tools/alerts, 0008 API vs CLI, 0009 hands gate, 0010 Elite, 0011 crew, 0012 DJ/music, 0013 hand-off, 0014 service cues/DJ turns, 0015 local library/blend.
- `docs/gui-api.md`: GUI server/frontend contract (endpoints, auth, SSE). Read before changing `packages/familiar/src/familiar/gui/`.
- `config.example.toml`: every config key with defaults.
- `packages/familiar/src/familiar/personas/`: packaged built-in persona YAML; user personas live in `~/.config/familiar/personas/` and shadow them.
- `packaging/linux/`: menu launcher + icon installer (`install.sh`).
- `packages/familiar/src/familiar/`: core voice loop, backends, Director and GUI; no imports from game packs.
- `packages/familiar-scs/src/familiar_scs/`: ATS/ETS2 adapters, commands, data and `knowledge/<game>/` packs.
- `packages/familiar-ksp/src/familiar_ksp/`: KSP kRPC telemetry, events, quotes and probe/record commands.
- `packages/familiar-elite/src/familiar_elite/`: Elite journal/status, events, `.binds` writer (`elite-bindings`), hands.
- `packages/familiar-music/src/familiar_music/`: DJ service (ADR 0012/0014/0015): Pear + mpv players, sources, `library/` index, `familiar music` CLI, data.
- `docs/knowledge-packs.md`: landmark pack format and the `familiar landmarks status|add|check` workflow. Read before adding or editing landmarks.
- `scripts/`: data builders; landmark scripts are thin wrappers around `packages/familiar-scs/src/familiar_scs/curation.py`.
- `packages/*/tests/`: per-package pytest suites.
