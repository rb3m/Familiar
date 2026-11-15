# Familiar

## What and why
Familiar is a voice companion with a personality (Mira, Ember, …) that chats with the player
and interjects about what is happening in sim games: landmarks near the truck in ATS, a pirate-
infested system in Elite, a burn in KSP. It also works away from games as a desk assistant on the
Mac. Everything is modular: LLM, speech-to-text, text-to-speech and game adapters are plugins
behind `typing.Protocol` interfaces, chosen in `config.toml`; personas are YAML data. Gemini (free
API tier) is the first backend; others plug in via entry points.

## Current step
- Milestone: **M0 Foundation** done offline (48 tests); live Gemini chat not yet verified.
- Next task: verify `uv run familiar chat` with a free-tier key, then **M1 Voice loop**: PushToTalk sources (evdev joystick, GlobalShortcuts portal key, terminal fallback), Gemini STT/TTS, ElevenLabs TTS, barge-in.
- Blockers: needs a free-tier `GEMINI_API_KEY` (AI Studio project without billing).

## Rules
- Python 3.12 via uv: `uv sync`, `uv run familiar …`. PEP-8; lint `uv run ruff check .`, format `uv run ruff format .`.
- Tests: `uv run pytest` (no network; live API tests marked `live`, run with `-m live`).
- Prefer early returns and mappings over nested ifs and `else`.
- Plugins depend only on `familiar.interfaces`; bump `PLUGIN_API_VERSION` only for breaking changes.
- Secrets only via env vars; never commit `config.toml` or keys.
- Internal dates ISO 8601; SI units in telemetry and knowledge packs.
- One ADR per real decision in `docs/decisions/NNNN-slug.md`; ordinary "why" goes in commit messages.

## Index
- `docs/architecture.md`: modules, interfaces, Director rules, platforms. Read before changing interfaces or adding a module.
- `docs/plugins.md`: how to add an LLM/STT/TTS/game backend. Read before writing a plugin.
- `docs/roadmap.md`: milestones and done-criteria. Read when picking the next task.
- `docs/decisions/`: ADRs. Read 0001 before changing the overall structure.
- `config.example.toml`: every config key with defaults.
- `personas/`: persona YAML files.
- `src/familiar/`: package; `backends/` holds built-in plugins.
- `tests/`: pytest suite.
