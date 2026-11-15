# Architecture

Familiar is an asyncio Python 3.12 application built from small modules behind interfaces.
Read the section you need; ADR 0001 records why it is shaped this way.

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
| Interfaces | `src/familiar/interfaces/` | `typing.Protocol`s and shared dataclasses. The only contract plugins depend on. |
| Plugins | `src/familiar/plugins.py` | Discovers backends via entry points, checks `PLUGIN_API_VERSION`. |
| Config | `src/familiar/config.py` | Loads `config.toml` (see `config.example.toml`). Secrets come from env vars. |
| Persona | `src/familiar/persona.py` | Loads `personas/*.yaml`; builds the system prompt for the active language. |
| Conversation | `src/familiar/conversation.py` | Owns history; streams LLM output, splits sentences, hands them to TTS. |
| Sentences | `src/familiar/pipeline/sentences.py` | Incremental sentence splitter with a max-length flush. |
| Transcript | `src/familiar/transcript.py` | Append-only JSONL log, the raw material for future memory. |
| Director | `src/familiar/director.py` | M2. Decides when the companion speaks. |
| Backends | `src/familiar/backends/<vendor>/` | Built-in plugins. Gemini first. |

## Interfaces (summary)

- **LLMProvider**: `stream(messages, system, tools) -> AsyncIterator[LLMChunk]` (text deltas and tool
  calls). Cancellation = cancelling the consuming task.
- **SpeechToText**: `transcribe(audio: AudioClip, language) -> str`. Push-to-talk clips, not
  continuous streams, until a streaming backend needs it.
- **TextToSpeech**: `synthesize(text, voice, language) -> AsyncIterator[AudioChunk]` (PCM16 mono).
- **GameAdapter** (M2): `events() -> AsyncIterator[GameEvent]`, `snapshot() -> dict`, optional
  `tools()`. Each game ships a knowledge pack under `games/<game>/`.
- **Conversation** (later): a higher-level interface so a speech-to-speech backend (Gemini Live) can
  replace the whole STT→LLM→TTS chain. The Director talks to Conversation, never to raw backends.

Every plugin class exposes `name`, `api_version` and `capabilities` (a set of strings such as
`"tools"`, `"streaming"`), and an optional async `aclose()`.

## Speech arbitration rules (Director, M2)

- Priorities: `critical` (fuel, damage) > `notable` (POI, new system) > `ambient` (idle chat).
- Pressing push-to-talk interrupts playback and cancels generation.
- Queued interjections carry a deadline; stale ones are dropped, not spoken late.
- Per-persona `chattiness` scales cooldowns for `ambient` and `notable`.

## Grounding

POIs and lore come from curated knowledge packs (with source, map/DLC and coordinates). The prompt
gives the model the matched facts. How freely it may embellish them is the persona's
`truthfulness` (`persona.py`): `strict` (Mira) never invents, while `playful` (Ember) may
exaggerate places and lore as obvious hyperbole but answers straight when sincerely asked. In every
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
