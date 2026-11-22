# Architecture

Familiar is an asyncio Python 3.12 application built from small modules behind interfaces.
Read the section you need; ADR 0001 records why it is shaped this way.

## Installation

For development, run `uv sync --all-packages` from the workspace root, then `uv run familiar gui`.
Users install the core straight from GitHub (not on PyPI yet) and add game packs the same way;
the core works without any game pack. The commands are in `README.md`.

## Overview

```
 Game adapters ──events + snapshot──► DIRECTOR ──speak request──► CONVERSATION
 (ATS, ETS2, KSP, MSFS, Elite)        priorities, cooldowns,       │
   + knowledge packs (YAML POIs)      stale-drop, PTT barge-in     ├─ LLMProvider  (Gemini, …)
                                            ▲                      ├─ Persona      (YAML data)
 Mic ── push-to-talk ── SpeechToText ───────┘                      └─ TextToSpeech (Gemini, ElevenLabs, …)
                                                                          │
 Transcript log (JSONL, every event and utterance) ◄──────────────────────┘ ──► speakers
```

Current accounts: Gemini flash-lite runs on the free API key (429s park a model for 15 min,
then `fallback_api_key_env`); ElevenLabs is on the Pro plan, with Gemini TTS as the fallback.

## Modules

| Module | Path | Role |
|---|---|---|
| Interfaces | `packages/familiar/src/familiar/interfaces/` | `typing.Protocol`s and shared dataclasses. The only contract plugins depend on. |
| Plugins | `packages/familiar/src/familiar/plugins.py` | Discovers backends via entry points, checks `PLUGIN_API_VERSION`. |
| Config | `packages/familiar/src/familiar/config.py` | Loads `config.toml` (see `config.example.toml`). Secrets come from env vars. |
| Persona | `packages/familiar/src/familiar/persona.py` | Loads user, configured and packaged persona YAML; builds the system prompt for the active language. |
| Session settings | `packages/familiar/src/familiar/session_settings.py` | Validates and atomically saves versioned launcher selections; CLI/env choices override them. |
| Crew | `packages/familiar/src/familiar/crew.py` | Ordered roster, controls (carrier) and DJ roles, hand-off targets, guest rotation, alias/follow-up addressing and event interests. |
| History | `packages/familiar/src/familiar/history.py` | Attributed shared records and a swappable renderer for each speaker. |
| Conversation | `packages/familiar/src/familiar/conversation.py` | Owns history; streams LLM output, splits sentences, hands them to TTS. |
| Sentences | `packages/familiar/src/familiar/pipeline/sentences.py` | Incremental sentence splitter with a max-length flush; never cuts inside `*…*` or `[…]`. |
| Speech | `packages/familiar/src/familiar/speech.py` | What the voice says: markup rule for prompts, stage-direction filter (`data/speech.yaml`). |
| Transcript | `packages/familiar/src/familiar/transcript.py` | Append-only JSONL log in `data/transcripts/` (lines, tool calls/results, events): the raw material for memory, and the first place to look when debugging a live session. |
| Director | `packages/familiar/src/familiar/director.py` | Decides when the companion speaks (priorities, TTL, cooldowns, idle chatter). |
| Memory | `packages/familiar/src/familiar/memory/` | SQLite trip facts, local retrieval, gated recall and shutdown summaries (ADR 0005). |
| GUI | `packages/familiar/src/familiar/gui/` | Local web launcher: aiohttp server owning one voice subprocess, vanilla frontend in `static/` (ADR 0003, `docs/gui-api.md`). |
| Desk | `packages/familiar-desk/src/familiar_desk/` | `desk-feed` helper (pluggable sources, atomic JSON feed) and the `desk` companion service (ADR 0016). |
| System | `packages/familiar-system/src/familiar_system/` | `system` service: platform readers (own hwmon reader on Linux, psutil elsewhere), data-driven sensor rules, once-per-crossing `system_notice` alerts, `system_status` tool; `familiar system sensors` inventory (ADR 0025). |
| Research | `packages/familiar-research/src/familiar_research/` | `research` service: background vault/web lookups through `agy`, answered as `research_result` cues; `familiar research check` permission self-test (ADR 0023). |
| SCS games | `packages/familiar-scs/src/familiar_scs/` | ATS/ETS2 adapter: telemetry, projection, places, landmark packs, event rules, `describe()`. |
| Backends | `packages/familiar/src/familiar/backends/<vendor>/` | Built-in plugins. Gemini first. |

Outside-owned core defaults live in packaged `data/*.yaml`, loaded through `interfaces.load_data` with uncached user overrides in `~/.config/familiar/data/` (`FAMILIAR_DATA_DIR` overrides the root).

## Interfaces (summary)

- **LLMProvider**: `stream(messages, system, tools) -> AsyncIterator[LLMChunk]` (text deltas and tool
  calls). Cancellation = cancelling the consuming task.
- **SpeechToText**: `transcribe(audio: AudioClip, language) -> str`. Push-to-talk clips, not
  continuous streams, until a streaming backend needs it.
- **TextToSpeech**: `synthesize(text, voice, language) -> AsyncIterator[AudioChunk]` (PCM16 mono).
  It receives `speech.speakable()` text: stage directions cut, emphasis unwrapped, `[tags]`
  removed unless its `capabilities` include `"audio_tags"`, and tool calls written as text are
  never spoken (ADRs 0020, 0021). Transcripts keep the
  raw text.
- **GameAdapter** (M2): `events() -> AsyncIterator[GameEvent]`, `snapshot() -> dict`, optional
  `tools()`. Each game pack ships its own knowledge data.
- **Conversation** (later): a higher-level interface so a speech-to-speech backend (Gemini Live) can
  replace the whole STT→LLM→TTS chain. The Director talks to Conversation, never to raw backends.

Backend plugin classes expose `name`, `api_version` and `capabilities` (a set of strings such as
`"tools"`, `"streaming"`), and an optional async `aclose()`.

## Speech arbitration rules (Director, M2)

- Priorities: `critical` (fuel, damage) > `notable` (POI, new system) > `ambient` (idle chat).
- Pressing push-to-talk interrupts playback and cancels generation.
- Queued interjections carry a deadline; stale ones are dropped, not spoken late.
- Per-persona `chattiness` scales cooldowns for `ambient` and `notable`.
- A `cue` event (e.g. a song change) skips those cooldowns and the per-kind gap but still waits
  until nobody is speaking or holding push-to-talk; its short TTL drops it if the moment passes.
- `speaker` names a crew role (`dj`, `controls`, legacy `carrier`) whose holder takes the event,
  or `guest` (the rest of the crew in rotation, granted the event's offered tools);
  `tools` offers named autonomous tools on that one unprompted turn (other interjections stay
  tool-free). See ADR 0014, 0018.

Game packs may attach `event.data["flavor"]`, a list of real quote candidates with `id`, `text`,
`speaker`, `context` and `attribution` (`verified` or `attributed`). Packs supply candidates;
core `FlavorKeeper` gates them: no critical events, a 20% chance, a 30-minute global cooldown
and a 14-day cooldown per quote ID per game. The model may loosely paraphrase a fitting quote,
crediting its speaker and hedging uncertain attribution. Use is recorded after speech, in the
shared memory database (or only in process memory when trip memory is disabled).

## Tools and alerts

Player turns offer core `ToolRegistry` tools to the LLM. Conversation retains assistant text
and structured calls, validates and executes handlers, then supplies JSON tool results for the
next response. Execution is bounded to three tool rounds followed by a tool-free response;
interjections offer no tools. History trimming keeps each assistant call/results group intact,
and the transcript records calls and results, including errors and interruptions.

Voice sessions can select one to three personas from `session.toml` (ADR 0011, phase 1).
Each turn has one speaker and uses that persona's voices, including speech fallbacks. The
shared history stores speaker IDs and whether text was heard; voice acknowledges a sentence
after audio drains. Each speaker sees her own model/tool turns and other members in a labelled
transcript preface with compact tool outcomes. Single-persona prompts retain their existing
form. Tools belong to a crew role (ADR 0018): `controls` (mutating actions and confirmation,
the carrier) or `dj` (music); each is offered and dispatched only on its holder's turns, while
core tools, `now_playing` and `read_only` actions are available to everyone. A speaker lacking a
role in use gets `hand_off(to, line?)`: her own tool calls in that round run, an optional remark
uses her voice, then the target handles the same request in the same reply (ADR 0013). A hand-off
never returns to anyone who already spoke in that reply, so chains end within the crew size.
The target's first round must be a tool call (or a further hand-off) when the LLM backend
supports `require_tool`, so she acts instead of describing the job (ADR 0021).
For crews of two or three, session banter levels 1-10 replace idle remarks with one tool-free
call for a validated 2-4-line scene. `banter.py` owns the gap/cap anchors (1: 1200 s, 2/h;
5: 420 s, 5/h; 10: 120 s, 10/h), linear interpolation with caps rounded down, and a rolling
hour budget. Only the quiet gap scales by `1.5 - mean chattiness`. Queued items and all speech
reset quiet time. Level 0 and single-member crews retain idle chatter.

Scene initiators are weighted by chattiness; partners with a relationship in either direction
have triple weight. Consecutive ordered pairs rotate. Both cast members must speak; a third
may contribute one line. Each line uses the ordinary serial playback path in its own voice.
Barge-in and critical preemption discard the remainder, retaining every validated line with
its speaker, `kind=banter`, heard status and `persona_claim` provenance. Unheard banter never
appears as a spoken model turn or factual memory. Malformed scenes consume no scene budget;
a 30-second admission retry delay bounds repeated invalid generations.

Pending hands confirmations and backend budget pressure suppress scenes. Backends may expose
a read-only `banter_blocked()` hook, consumed through `fallback.llm_budget_blocked`, without
changing the plugin protocol. Gemini reports any live model park and estimates request usage
against optional configured per-model `quota_limits` (`minute`/`day`). The near-limit threshold
is 90%; retries and fallback attempts count conservatively across keys, over rolling windows.
These are process-local estimates, not an account-wide quota API. Without configured limits,
Gemini still suppresses scenes on parking. Desk sessions run the Director without a game feed.

Adapters optionally implement `Watchable`, describing quantities in SI and returning cached
samples with monotonic acquisition times, validity, prediction safety and a context identity.
Core `AlertKeeper` supplies `set_alert`, `list_alerts` and `cancel_alert` only for those adapters
when `[alerts]` is enabled. Up to eight session-only alerts bind to the current vessel/job;
context changes drop them, and invalid samples suspend and reset their crossing baseline.
A task beside `feed_events` polls at 2 Hz only while alerts exist. Directional crossings include
equality and jumps, with a tiny arming hysteresis. Stable, approaching rates from three recent
slopes may predict a crossing within `lead_s` to cover speech latency; warp, irregular spacing
and unstable rates fall back to actual crossings. One-shot critical `alert` events carry a
10-second TTL and zero memory salience; the Director keys them by alert ID.

## Memory

Core memory stores sessions, salient episodes and per-persona recall history in one local SQLite
database (WAL), scoped by game. Adapters optionally supply `whereabouts()`, `memory_salience` and
`memory_arrival`; these hooks do not change the required plugin interface. Player turns receive
up to three dated earlier-session episodes using place/region and accent-insensitive entity
matching, plus up to two summaries on recall cues. Retrieval never calls a backend. Spontaneous
recall requires an arrival, salience ≥ 0.5, a 45-minute global cooldown, a seven-day episode/persona
cooldown and a 25% probability gate; history updates when the reply is consumed (after audio in
voice mode). Clean shutdown summarizes transcript utterances with the configured LLM, bounded
to 15 seconds, before closing it. Desk sessions retain summaries without game episodes.
Schema 3 attributes rows to speakers and separates game/player observations from persona claims.
Claims are excluded from factual retrieval and new factual summaries. Both the global recall
gate and the seven-day episode gate apply across the active roster.

## Grounding

POIs and lore come from curated knowledge packs (with source, map/DLC and coordinates). The prompt
gives the model the matched facts. How freely it may embellish them is the persona's
`truthfulness` (`persona.py`): strict personas never invent; playful personas may exaggerate
places and lore as obvious hyperbole but answer straight when sincerely asked. Mira uses strict
truthfulness. In every
mode, practical information the player acts on (fuel, damage, distances, deadlines) stays exact.

## Platforms

- Linux (KDE Wayland) and macOS Apple Silicon. Audio via `sounddevice` (needs system PortAudio on Linux).
- Push-to-talk is mappable to a joystick/HOTAS button **or** a keyboard key (KSP plays best on
  keyboard). It is a `PushToTalk` source interface with platform backends:
  - Linux joystick: evdev. Joystick event nodes are already readable through systemd-logind
    `uaccess`, no extra groups.
  - Linux keyboard (**deferred**: the user moves the HOTAS beside the keyboard for KSP, so M1 ships
    joystick + terminal only): the xdg-desktop-portal **GlobalShortcuts** portal (KDE supports it; it signals
    press *and* release). This avoids the `input` group, which would let any process keylog.
    Fallback: a udev rule granting `uaccess` to one dedicated device (e.g. a gaming keypad).
  - macOS: `mouse` backend, an active Quartz session tap that consumes one spare mouse button
    (Accessibility permission, attributed to Terminal or Familiar.app; ADR 0017). Assigned with
    `ptt-setup` or the GUI panel through one shared detector. A keyboard hotkey is not built yet.
  - Terminal key (hold Space) as a no-permission fallback for development.
- The user is on open speakers about half the time, so the mic only records while PTT is held, and
  pressing PTT stops playback (barge-in). Without that, the companion would hear itself.
- Key injection ("hands", M4) uses physical scancodes via uinput, never characters (latam layout).


## Actions (hands)

Optional `Actuator` packs expose action specs and execution; core `ActionKeeper` registers
player-only tools, deduplicates each turn and bounds execution with a timeout. Risky actions
require a later affirmative player turn within a TTL and unchanged context; negatives cancel,
clarifying turns preserve pending actions. Pending consent is consumed before execution.
Core owns the optional `Keyboard` backend (`familiar.keyboard`, initially Linux uinput),
serialises chords and releases keys on cancellation and shutdown. Packs own game safety and
bindings. See ADR 0009; these additive protocols retain API 2.0.

## Companion services

API 2.1 adds `CompanionService` alongside the game adapter: a `familiar.services` plugin
provides an asynchronous event stream, `ServiceTool` specifications and handlers (including
carrier-only visibility), a cached `describe()` line, a `SpeechState` hook and async cleanup.
Core registers tools in the conversation and combines service context with game context.
Voice serializes listening/speaking/critical changes, including the active interjection kind,
and waits for coordination before playback. Hook/feed/cleanup failures are logged and isolated.
Service events retain Director deadlines, busy gates and cooldowns but bypass game connection
and pause gates. A service also creates a desk Director when there is no game or crew banter.

The optional `[music]` service is disabled by default. `familiar-music` owns Pear playback,
threaded YouTube Music discovery, radio selection and a single duck/pause coordinator.
Track remarks pause music; ordinary speech ducks against a separately remembered base volume.
Remarks come every 3-4 natural song changes and the DJ gets a DJ turn (may call `play_music`)
every 8-12; every third heard DJ turn is lent to another crew member in rotation as a guest pick
(`guest_pick_every`). DJ turns are held back for 15 minutes after any music request and while paused.
Critical pauses resume only while ownership survives explicit user controls and observed track
or playback changes. An external pause command issued while playback is already paused cannot
be distinguished through Pear's state-only API. Dislikes use injected MemoryStore callbacks,
independent of games/personas; disabled memory uses process-local taste instead.

The optional `[desk]` service (`familiar-desk`, ADR 0016) runs only in desk sessions, never
beside a game adapter (ADR 0022). It starts the `familiar desk-feed` helper
unless another writer holds its lock, and stops it on close. The helper samples each source on
its own interval into a versioned envelope (`sources` plus a bounded `events` list with stable
ids) written atomically to `<runtime>/familiar/desk-feed.json`. The service turns unseen events
into ambient remarks and adds the local time and weather to the prompt. Sources are plugins
(`familiar_desk.sources`) chosen by `[desk] sources`: the clock and, hourly, the weather
(ADR 0019), which asks pluggable `familiar_desk.weather_providers` (Open-Meteo, METAR) in order,
later ones filling gaps. RSS, email and the other sources from the roadmap plug in the same way.

The optional `[research]` service (`familiar-research`, ADR 0023) runs in desk and game sessions.
Its `research(question, scope)` tool returns "started" at once and runs one `agy` lookup in the
background (argv only, private temp dir, keys stripped, per-scope allow/deny profile,
`--sandbox`, JSON schema); the answer arrives as a `notable` `research_result` cue keyed by
research id and is spoken in a tool-free turn. `research_details` answers follow-ups from a
masked, bounded session cache; deeper follow-ups resume the agy conversation.
Watched topics (ADR 0024) also live there: private topic files in
`$XDG_CONFIG_HOME/familiar/topics/`, checked silently by the research service at each topic's
check times (one agy call, per-area coverage, URL seen-set, rule-confirmed `action`) into
`$XDG_STATE_HOME/familiar/topics/<id>.json`; `topic_status` reads it. A structurally typed
`topics` desk source in the same package voices confirmed action items once, desk only.
