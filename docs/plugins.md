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
   `async stream(...)` and optional `async aclose()`. A TTS backend that acts out inline audio
   tags (`[sighs]`, ElevenLabs v3) adds `"audio_tags"` to `capabilities`; others never see
   tags (ADR 0020).
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

## Plugin API 2.0: structured tool history

API 2.0 is a breaking interface change; all in-repo plugins use the shared
`PLUGIN_API_VERSION` (currently `"2.1"`). External plugins must declare API major 2 after updating their
message translation; the loader rejects API 1 plugins.

`Message` retains `role` and `text` and adds `tool_calls: tuple[ToolCall, ...] = ()` for assistant
messages and `tool_call_id: str | None = None` for the new `"tool"` role. Tool result `text` is a
compact JSON object. `ToolCall(id, name, arguments)` adds `metadata: dict` with an independent
empty default. Metadata is opaque provider data and must survive the round trip.

An LLM provider must translate assistant text and calls together and match each result to its
call ID. Gemini maps these to model `function_call` parts and user `function_response` parts
with `id`, matching function `name`, and the parsed JSON `response`. Its function-call Part's
`thought_signature` is preserved as base64 in `ToolCall.metadata["thought_signature"]` and
decoded back to SDK bytes when sending history. Do not discard it: Gemini 3 requires it for
subsequent tool turns. Specs are supplied only on player turns; an empty tool list means no
new calls are permitted, though history may still contain earlier calls and results.

## Optional telemetry alerts: Watchable

Packs may implement the runtime-checkable `familiar.interfaces.Watchable` independently of
`GameAdapter`. Core owns alert tools, conversions, crossing/prediction logic and scheduling;
packs only describe and sample quantities:

```python
from familiar.interfaces import WatchSample, WatchSpec


def watchables(self) -> dict[str, WatchSpec]:
    return {"altitude": WatchSpec("m", "altitude above sea level")}


def watch_sample(self) -> WatchSample:
    return WatchSample(
        values={"altitude": self.latest.altitude_m},
        context=self.latest.vessel_id,
        valid=self.latest.in_flight and self.latest.paused is False,
        predictable=self.latest.warp_rate == 1,
        timestamp=self.last_acquired_monotonic,
    )
```

Units are `m`, `m/s`, `s`, `%` (0–100), or `count`; unavailable quantities return `None`, never
invented zeros. Descriptions distinguish remaining navigation distance from straight-line
distance and game time from real time. Samples must be fast, nonblocking cached reads, with a
monotonic timestamp updated on acquisition rather than on each read. Context is the current
vessel ID, job source/destination identity or `"free roam"`; a change cancels bound alerts.
Disconnected/paused/out-of-flight samples are invalid, and warp must disable prediction.
Core converts m/km/ft/mi/nmi, m/s/km/h/mph/kn, s/min/h and percentages deterministically.

### Quantities in tool results

Tool results give quantities as `spoken(value, unit)` strings in the game's native units.
Never return bare numbers with unit suffixes; format only after filtering and ranking.
Internal telemetry and GameEvents stay numeric; Watchables stay numeric SI.

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

KSP quotes live in `knowledge/quotes.yaml` (19 curated, source-checked; each entry also carries `source_url` and
event-kind `triggers`, which the loader validates and does not attach to events).

KSP resource definitions live in `data/resources.yaml` (vessel and stage mappings, fallback
propellants, and spoken display names for stock and Community Resource Pack fuels). Users can
override or extend resource definitions at `~/.config/familiar/data/familiar_ksp/resources.yaml`.
At runtime, telemetry discovers the propellants burned by active or next-to-fire engines via kRPC
(`Engine.propellant_names`, falling back to `fallback_propellants` when unavailable). Propellant
names are cached per `(vessel, stage)` and refreshed on stage changes. Telemetry provides generic
`stage_propellants` (per-propellant amount/max) and derives `stage_propellant_fraction` (the
minimum remaining fraction across discovered propellants), exposed as the `stage_propellant` watchable
(`%`). Unknown resource names are converted to spoken English by splitting CamelCase.

KSP hands (actions) implement `Actuator` through a dedicated kRPC connection (`"Familiar actions"`)
on its own single-worker thread, opened lazily and bounded by `action_timeout_s` (default 3 s).
Supported actions cover staging, abort, SAS state and mode steering, RCS, landing gear, lights,
brakes, action groups 1–10, throttle, and on-rails time warp. Risky commands require spoken
confirmation under ADR 0009 and refuse execution if the active vessel changes or if flight safety
guards are unmet.


## Actions (hands)

A pack optionally implements `Actuator` from `familiar.interfaces`: `actions()` returns
names mapped to `ActionSpec(description, parameters, risky=False, risky_when=None)`;
`async perform(name, arguments)` returns a dict with `status` and `detail`. Status values
are `done`, `already`, `unavailable`, `unknown`, and `error`. Parameters use the core tool
JSON-schema subset. Set `risky` for confirmation, or supply `risky_when(arguments)` to
override it per call. Optional `action_context()` returns a fast identity (`str | None`)
that core checks again before confirming. Packs must re-check game safety before acting.

Optional `ActionSpec.timeout_s` overrides the core `action_timeout_s` for that action.

Core offers `act_<name>` only on player turns and owns confirmation, timeouts and deduplication.
A `KeyboardUser` implements `attach_keyboard(keyboard)`; core loads `familiar.keyboard`
(default `uinput`) and owns cleanup. Use `await keyboard.tap(["leftctrl", "o"], hold_s=0.05)`
with lowercase evdev names without `KEY_`; chords release in reverse order. Unknown keys
raise `ValueError`. Packs own bindings and toggle semantics and return `unavailable` when
no keyboard is attached. The additive interfaces retain plugin API 2.0 (ADR 0009).


## Data files and overrides

Ship outside-owned defaults in `data/<name>.yaml` inside your package. Packs use
`from familiar.interfaces import load_data` and `load_data("my_pack", "catalog")` with their
own package name; identifiers may contain only lowercase letters, digits and underscores.
The returned `DataSet.value` merges packaged data with
`~/.config/familiar/data/<package>/<name>.yaml`; `FAMILIAR_DATA_DIR` overrides the root on all
platforms. `DataSet.sources` lists the packaged path followed by any user file so validation
errors can name their sources. Validate the merged shape in the caller.

Put a header comment in each packaged file explaining its purpose and override path; users
need only write keys they change. Mappings merge recursively, lists/scalars replace, and null
mapping values delete keys. Empty user files leave defaults intact; duplicate mapping keys
are errors. Quote string keys such as `"yes"` and `"no"` to avoid YAML boolean coercion.
`user_data_path` locates overrides and `save_user_data` writes them atomically. Loads are
uncached: call `load_data` again to refresh; existing backend instances and action keepers
pick up defaults when recreated.


SCS ships its shared ATS/ETS2 action catalogue in `actions.yaml`, the default keyboard
layout in `default_keys.yaml`, key/modifier translations in `key_names.yaml`, projection
calibration in `projections.yaml`, state/country names in `regions.yaml`, and suggestion
exclusions in `ignored_controls.yaml`. Override these under
`~/.config/familiar/data/familiar_scs/` (or `$FAMILIAR_DATA_DIR/familiar_scs/`); restart the
adapter to reload data overrides. Profile bindings refresh automatically before key presses
when the discovered controls file changes, appears or disappears; a failed parse retains the
previous table and logs a warning. Settings `keys` overrides still apply. Inspect with
`familiar scs bindings ats|ets2 [--profile P] [--suggest]`; `P` accepts a profile name,
directory or controls file. `--suggest` merges risky draft actions for unused keyboard-bound
controls into the user's `actions.yaml`, preserving existing entries. Review descriptions,
risk and state behavior before restarting Familiar to use drafts. Missing profile controls
remain unbound; only sessions without a controls file use packaged default keys.

## Add a companion service

Implement `familiar.interfaces.CompanionService` and register a `familiar.services` entry point.
`events()` yields ordinary `GameEvent` values without a game snapshot; core marks their origin
so game connection/pause gates do not suppress them. `service_tools()` returns `ServiceTool`
values with JSON-schema parameters, async dictionary-returning handlers and an owning crew
`role` (`"controls"`, `"dj"` or `None` for everyone; `carrier_only=True` means `"controls"`).
`describe()` must return cached prompt context quickly, or an empty string. `speech_state()`
receives complete immutable listening/speaking/critical state and the spoken event kind;
make repeated states idempotent and restore resources in `aclose()`. Core isolates failures.
API 2.1 is additive and remains compatible with API major 2 plugins.
API 2.2 (additive, ADR 0014): a service event may set `data["cue"]` (tied to a moment: skip the
Director's quiet gaps, still wait for silence), `data["speaker"] = "carrier"` (route to the
carrier), `data["tools"]` (tool names offered on that unprompted turn; only tools registered with
`ServiceTool(autonomous=True)` are honoured, role rules still apply) and `data["taste"]`
(a persona `tastes` topic added as hints).
API 2.3 (additive, ADR 0018): `ServiceTool.role`; `data["speaker"]` names a role (`"dj"`,
`"controls"`, or `"carrier"` as an alias for controls) or `"guest"`: the next crew member other
than the DJ, in rotation, who may use that event's offered autonomous tools for the one turn. `speech_state()` seeing `speaking` with your event kind
means the Director admitted that event; use it to restart schedules instead of counting emissions. `[music]` selects the first
service; settings other than `enabled` and `service` pass through to its constructor. Core
injects `dislike_read()` and `dislike_write(video_id, artists)` callbacks when memory is enabled;
plugins must import only `familiar.interfaces`, never core memory or voice implementations.

## Add a desk source or weather provider

The `familiar-desk` pack has its own two extension points (ADR 0019). They live in the pack,
not in `familiar.interfaces`, because core never sees them; a plugin for either depends on
`familiar_desk`.

| Group | Interface | Example |
|---|---|---|
| `familiar_desk.sources` | `familiar_desk.sources.Source` | `weather = "familiar_desk.sources.weather:WeatherSource"` |
| `familiar_desk.weather_providers` | `familiar_desk.weather.WeatherProvider` | `metar = "familiar_desk.weather.metar:MetarProvider"` |

A **source** is built from the whole `[desk]` table, has `name` and `interval_s`, and
`sample(now)` returns `(data, events)`: JSON-ready data under its name in the feed, and events
with stable `id`s (include the date or slot so a restart never repeats one), `ts`, `kind`,
`priority`, `summary` and `ttl_s`. Raising keeps the previous data and records the error. Users
enable it by name in `[desk] sources`. Add a matching line to `DeskService.describe` only if the
prompt should carry it.

A **weather provider** is built from the merged `[desk.weather]` settings (data defaults in
`familiar_desk/data/weather.yaml`, so put endpoints and code tables there) and implements
`async fetch(place, client) -> WeatherReport` with the shared `httpx.AsyncClient`. Fill what
the service knows, in SI units, and leave the rest `None`; raise `ValueError` or an
`httpx.HTTPError` when it cannot answer for that place. Users list it in `providers`, in
fallback order.
