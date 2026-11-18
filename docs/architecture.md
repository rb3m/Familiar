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

## Modules

| Module | Path | Role |
|---|---|---|
| Interfaces | `packages/familiar/src/familiar/interfaces/` | `typing.Protocol`s and shared dataclasses. The only contract plugins depend on. |
| Plugins | `packages/familiar/src/familiar/plugins.py` | Discovers backends via entry points, checks `PLUGIN_API_VERSION`. |
| Config | `packages/familiar/src/familiar/config.py` | Loads `config.toml` (see `config.example.toml`). Secrets come from env vars. |
| Persona | `packages/familiar/src/familiar/persona.py` | Loads user, configured and packaged persona YAML; builds the system prompt for the active language. |
| Session settings | `packages/familiar/src/familiar/session_settings.py` | Validates and atomically saves versioned launcher selections; CLI/env choices override them. |
| Crew | `packages/familiar/src/familiar/crew.py` | Ordered roster, carrier, alias/follow-up addressing and event interests. |
| History | `packages/familiar/src/familiar/history.py` | Attributed shared records and a swappable renderer for each speaker. |
| Conversation | `packages/familiar/src/familiar/conversation.py` | Owns history; streams LLM output, splits sentences, hands them to TTS. |
| Sentences | `packages/familiar/src/familiar/pipeline/sentences.py` | Incremental sentence splitter with a max-length flush. |
| Transcript | `packages/familiar/src/familiar/transcript.py` | Append-only JSONL log, the raw material for future memory. |
| Director | `packages/familiar/src/familiar/director.py` | Decides when the companion speaks (priorities, TTL, cooldowns, idle chatter). |
| Memory | `packages/familiar/src/familiar/memory/` | SQLite trip facts, local retrieval, gated recall and shutdown summaries (ADR 0005). |
| GUI | `packages/familiar/src/familiar/gui/` | Local web launcher: aiohttp server owning one voice subprocess, vanilla frontend in `static/` (ADR 0003, `docs/gui-api.md`). |
| SCS games | `packages/familiar-scs/src/familiar_scs/` | ATS/ETS2 adapter: telemetry, projection, places, landmark packs, event rules, `describe()`. |
| Backends | `packages/familiar/src/familiar/backends/<vendor>/` | Built-in plugins. Gemini first. |

Outside-owned core defaults live in packaged `data/*.yaml`, loaded through `interfaces.load_data` with uncached user overrides in `~/.config/familiar/data/` (`FAMILIAR_DATA_DIR` overrides the root).

## Interfaces (summary)

- **LLMProvider**: `stream(messages, system, tools) -> AsyncIterator[LLMChunk]` (text deltas and tool
  calls). Cancellation = cancelling the consuming task.
- **SpeechToText**: `transcribe(audio: AudioClip, language) -> str`. Push-to-talk clips, not
  continuous streams, until a streaming backend needs it.
- **TextToSpeech**: `synthesize(text, voice, language) -> AsyncIterator[AudioChunk]` (PCM16 mono).
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
form. Mutating action tools, including confirmation, are offered and dispatched only on the
carrier's turns; core tools and `read_only` actions are available to everyone. Banter is stored
in the session selection but has no behavior until phase 2.

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
  - macOS: a CGEventTap/pynput hotkey (Accessibility permission).
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
