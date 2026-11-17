# Roadmap

| Milestone | Goal | Done when |
|---|---|---|
| **M0 Foundation** | Repo, interfaces, plugin loader, config, personas, Gemini LLM, text chat | `uv run familiar chat` streams a persona reply from Gemini; tests and ruff pass |
| **M1 Voice loop** | Push-to-talk → Gemini STT → LLM → Gemini TTS, ElevenLabs as 2nd TTS; Linux + Mac | Spoken round trip under ~2 s on both machines |
| **M2 ATS** (done 2026-11-15) | ATS adapter (TruckSim GPS server :31377), Director, first knowledge pack | Companion mentions a POI and low fuel during a real drive without spamming |
| **M3 Memory** (in progress) | Local per-game trip memory: places, jobs and notable moments, session summaries; recall when asked ("have we been here before?") and, rarely, unprompted | "Remember last trip to Reno?" works; spontaneous recall is occasional, never every visit |
| **M4 More games** (ETS2 live 2026-11-16: countries, towns, landmarks; KSP pack built 2026-11-16, ADR 0006, awaiting live check) | ETS2 (shares ATS adapter), KSP (kRPC; flight events + rare paraphrased astronaut quotes), then MSFS, Elite (journal + EDSM) | Each has an adapter + knowledge pack |
| **M5 Hands & languages** | uinput actions as LLM tools, Touch Portal sync, Japanese practice mode | Voice command toggles something in-game |

Planned: **how the companion addresses the user.** A name or nickname the user picks (e.g.
"Skipper"), plus a formality level (casual, polite, formal) set globally in `config.toml` /
the GUI and overridable per persona. Each language maps the level to its own register:
Japanese plain form vs です/ます vs 敬語 and honorifics (さん, 様), Spanish tú vs usted, German
du vs Sie. English mostly changes tone. Lands with or before the Japanese practice mode (M5).

Nice to have: **the companion as DJ.** Whoever rides shotgun runs the music. A `familiar.music`
plugin group controls the user's player (Linux: MPRIS, e.g. Spotify, via D-Bus; macOS: Music
app) for skip, volume, pause and "something calmer" requests, and the in-game radio through key
presses once M5 hands exist. Persona-flavoured picks matched to the drive (night, mountain
pass, tight deadline) and the occasional comment on the current song.

Later options: Gemini Live speech-to-speech `Conversation` backend, local backends (whisper.cpp
Vulkan, Kokoro, Ollama), Mac desk assistant / note taker to an Obsidian vault, standard SCS telemetry
plugin for users without the TruckSim GPS port.

Before publishing to PyPI: rename the distributions (`familiar` is taken there by an unrelated
project), e.g. `familiar-companion` and `familiar-companion-scs`.

SCS events added 2026-11-16 (ATS and ETS2): tolls, inferred traffic jams, hard braking and
trailer collisions, after-rest check-in, time of day from sun height, rain hint from wipers.
The telemetry has no traffic, AI-vehicle or weather data; these are inferred from the player's
own truck. Open checks: solar-noon hour (`SOLAR_NOON_HOUR`), UK projection branch, Iceland map
(not in the gazetteer or landmarks yet), jam thresholds.
