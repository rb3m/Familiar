# Writing a plugin

Familiar finds backends through Python entry points, so a plugin can live in this repo
(`packages/familiar/src/familiar/backends/`) or in a separate pip-installable package.

## Entry point groups

| Group | Interface | Example |
|---|---|---|
| `familiar.llm` | `familiar.interfaces.llm.LLMProvider` | `gemini = "familiar.backends.gemini.llm:GeminiLLM"` |
| `familiar.stt` | `familiar.interfaces.stt.SpeechToText` | `gemini = "familiar.backends.gemini.stt:GeminiSTT"` |
| `familiar.tts` | `familiar.interfaces.tts.TextToSpeech` | `elevenlabs = "…"` |
| `familiar.commands` | `familiar.interfaces.command.Command` | `watch = "familiar_scs.commands:Watch"` |
| `familiar.games` | `familiar.interfaces.game.GameAdapter` | `ats = "familiar_scs.adapter:AtsAdapter"` |

## Steps (adding an LLM provider, e.g. Anthropic or Ollama)

1. Create `packages/familiar/src/familiar/backends/<vendor>/llm.py` (or your own package).
2. Implement the class with the attributes and methods in `interfaces/llm.py`:
   `name`, `api_version = PLUGIN_API_VERSION`, `capabilities`, `__init__(self, settings: dict)`,
   `async stream(...)` and optional `async aclose()`.
3. Read secrets from an env var named in your settings (never from the TOML file itself).
4. Register it in `pyproject.toml`:
   ```toml
   [project.entry-points."familiar.llm"]
   myvendor = "familiar.backends.myvendor.llm:MyVendorLLM"
   ```
   then `uv sync --all-packages` so the entry point is installed.
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

Personas are YAML, not plugins: copy `packages/familiar/src/familiar/personas/mira.yaml`, change the prompt and voices, and pick
`truthfulness: strict` or `playful`. Voice
ids are scoped by TTS backend and language, so one persona can sound right in English and Japanese.

## Adding a command

Implement `familiar.interfaces.command.Command` in your package. The loader constructs the class
without arguments; it must expose `name`, `help`, and `api_version = PLUGIN_API_VERSION`.
`add_arguments(parser)` registers arguments on an argparse subparser; core supplies `--config`.
`run(args, config)` receives the parsed namespace and loaded application config, and returns an
integer exit status, either directly or from an async method. Register the class:

```toml
[project.entry-points."familiar.commands"]
watch = "familiar_scs.commands:Watch"
```

The entry-point name must match the class's `name`. Commands use the same API major-version
check as other plugins. Import, construction, and argument-registration failures produce a
warning on stderr and skip that command, leaving the other CLI commands available.

## Writing a game pack

Use `packages/familiar-scs/` as an example: it is a separate distribution with its own top-level
`familiar_scs` package, tests, telemetry data and knowledge packs. Core never imports a game pack;
it discovers installed adapters through `familiar.games` and optional CLI tools through
`familiar.commands`. Users without game packs can still run `familiar plugins`, `familiar gui`
and desk conversations.

Implement `GameAdapter` from `familiar.interfaces`, including `events`, `snapshot`, `describe`,
`tools`, and cleanup. Set `api_version = PLUGIN_API_VERSION` and expose `display_name` and `icon`
as class attributes so the GUI can read them without constructing or connecting an adapter.
Missing metadata falls back to the title-cased game ID and 🎮. Register each supported game:

```toml
[project.entry-points."familiar.games"]
ats = "familiar_scs.adapter:AtsAdapter"
ets2 = "familiar_scs.adapter:Ets2Adapter"
```

A pack may attach `event.data["flavor"]` as a list of candidate dictionaries:

```python
{
    "id": "shepard-light-this-candle",
    "text": "Let's light this candle",
    "speaker": "Alan Shepard",
    "context": "Freedom 7, 1961",
    "attribution": "verified",
}
```

`id` is a stable slug unique within the game; `text` is the real quote, `speaker` names who
said it, and `context` identifies its mission/year or equivalent setting. `attribution` must
be `verified` or `attributed` (popularly credited, origin uncertain). Attach candidates only;
packs never gate them. Core skips critical events, applies probability and global/per-item
cooldowns, and records use after speech. It offers a loose paraphrase in the companion's own
voice with natural credit, hedging `attributed` lines, never a long quotation.

Keep all game-specific defaults, dependencies and resources inside the pack. Ship data as package
data and read it with `importlib.resources`. Familiar SCS pins `familiar>=0.1.0,<1.0.0` to keep
core within the same distribution major version; the runtime plugin API check is separate.
For another pack in this workspace, add a directory under `packages/` and declare
`familiar = { workspace = true }` in `[tool.uv.sources]`. Run `uv sync --all-packages` to install
its entry points for development. External packs can be published and installed independently.

Personas are searched in the user directory first, then an optional configured `personas_dir`,
then the packaged built-ins. A missing configured directory is skipped. Save edits in the user
directory; using a built-in ID creates an override that the GUI can reset by deleting that copy.

### KSP pack

`familiar-ksp` uses the kRPC 0.7 client with polling on one worker thread and no streams.
The KSP kRPC mod must auto-start its server and auto-accept clients. `[games.ksp]` accepts
`address` (127.0.0.1), `rpc_port` (50000), `timeout_s` (3), `poll_interval_s` (0.5),
`replay` (JSONL path), `replay_speed` (1), and an optional `quotes_path` override.
`familiar ksp-probe` connects once; `familiar ksp-record FILE` records timestamped snapshots.
Replays accept plain snapshot dictionaries or `{ts, snapshot}` records, with timestamps in seconds.
Resources use KSP resource units; distances and speeds use SI units.

A pack may attach `event.data["flavor"]`, a list of quote candidates with string fields
`id`, `text`, `speaker`, `context`, and `attribution` (`verified` or `attributed`).
IDs are stable slugs unique within the game. Packs attach candidates only; core owns selection
and recording of use. KSP's packaged `knowledge/quotes.yaml` is intentionally empty. Its loader
also requires `source_url` and validated event-kind `triggers`, neither of which is attached
to events. Supply curated real quotes with mission/year context and source attribution.
