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
| Conversation | `packages/familiar/src/familiar/conversation.py` | Owns history; streams LLM output, splits sentences, hands them to TTS. |
| Sentences | `packages/familiar/src/familiar/pipeline/sentences.py` | Incremental sentence splitter with a max-length flush. |
| Transcript | `packages/familiar/src/familiar/transcript.py` | Append-only JSONL log, the raw material for future memory. |
| Director | `packages/familiar/src/familiar/director.py` | Decides when the companion speaks (priorities, TTL, cooldowns, idle chatter). |
| Memory | `packages/familiar/src/familiar/memory/` | SQLite trip facts, local retrieval, gated recall and shutdown summaries (ADR 0005). |
| GUI | `packages/familiar/src/familiar/gui/` | Local web launcher: aiohttp server owning one voice subprocess, vanilla frontend in `static/` (ADR 0003, `docs/gui-api.md`). |
| SCS games | `packages/familiar-scs/src/familiar_scs/` | ATS/ETS2 adapter: telemetry, projection, places, landmark packs, event rules, `describe()`. |
| Backends | `packages/familiar/src/familiar/backends/<vendor>/` | Built-in plugins. Gemini first. |

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
