# Familiar

## What and why
Familiar is a voice companion with a personality (Mira, Ember, …) that chats with the player
and interjects about what is happening in sim games: landmarks near the truck in ATS, a pirate-
infested system in Elite, a burn in KSP. It also works away from games as a desk assistant on the
Mac. Everything is modular: LLM, speech-to-text, text-to-speech and game adapters are plugins
behind `typing.Protocol` interfaces, chosen in `config.toml`; personas are YAML data. Gemini (free
API tier) is the first backend; others plug in via entry points.

## Current step
- Milestone: **M1 Voice loop** (M0 done; live chat verified 2026-11-15 on the free tier).
- Next task: PushToTalk sources (evdev HOTAS button + terminal fallback; keyboard deferred), Gemini STT/TTS, ElevenLabs TTS, barge-in.
- Known: free-tier `gemini-3.8-flash` often stalls or returns 503/504, so the default is `gemini-3.5-flash-lite` (~1 s) with retries, an 8 s first-chunk timeout and fallback. The user's local config reads `GEMINI_FREE_KEY`.

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
