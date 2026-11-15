# Writing a plugin

Familiar finds backends through Python entry points, so a plugin can live in this repo
(`src/familiar/backends/`) or in a separate pip-installable package.

## Entry point groups

| Group | Interface | Example |
|---|---|---|
| `familiar.llm` | `familiar.interfaces.llm.LLMProvider` | `gemini = "familiar.backends.gemini.llm:GeminiLLM"` |
| `familiar.stt` | `familiar.interfaces.stt.SpeechToText` | `gemini = "familiar.backends.gemini.stt:GeminiSTT"` |
| `familiar.tts` | `familiar.interfaces.tts.TextToSpeech` | `elevenlabs = "…"` |
| `familiar.games` | `familiar.interfaces.game.GameAdapter` | `ats = "…"` (M2) |

## Steps (adding an LLM provider, e.g. Anthropic or Ollama)

1. Create `src/familiar/backends/<vendor>/llm.py` (or your own package).
2. Implement the class with the attributes and methods in `interfaces/llm.py`:
   `name`, `api_version = PLUGIN_API_VERSION`, `capabilities`, `__init__(self, settings: dict)`,
   `async stream(...)` and optional `async aclose()`.
3. Read secrets from an env var named in your settings (never from the TOML file itself).
4. Register it in `pyproject.toml`:
   ```toml
   [project.entry-points."familiar.llm"]
   myvendor = "familiar.backends.myvendor.llm:MyVendorLLM"
   ```
   then `uv sync` so the entry point is installed.
5. Select it in `config.toml`:
   ```toml
   [llm]
   backend = "myvendor"
   [llm.myvendor]
   model = "…"
   api_key_env = "MYVENDOR_API_KEY"
   ```
6. Add tests with a fake HTTP layer; run `uv run pytest`.

## Rules

- Translate vendor errors into `familiar.interfaces.errors.BackendError` (retryable flag set for
  rate limits and timeouts).
- Streaming must be cancellable: no blocking calls in the event loop (use the vendor's async client
  or `asyncio.to_thread`).
- Bump `PLUGIN_API_VERSION` in `interfaces/__init__.py` only for breaking interface changes; the
  loader refuses plugins with a different major version.

## Personas

Personas are YAML, not plugins: copy `personas/mira.yaml`, change the prompt and voices, and pick
`truthfulness: strict` or `playful`. Voice
ids are scoped by TTS backend and language, so one persona can sound right in English and Japanese.
