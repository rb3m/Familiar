# Roadmap

| Milestone | Goal | Done when |
|---|---|---|
| **M0 Foundation** | Repo, interfaces, plugin loader, config, personas, Gemini LLM, text chat | `uv run familiar chat` streams a persona reply from Gemini; tests and ruff pass |
| **M1 Voice loop** | Push-to-talk → Gemini STT → LLM → Gemini TTS, ElevenLabs as 2nd TTS; Linux + Mac | Spoken round trip under ~2 s on both machines |
| **M2 ATS** (done 2026-11-15) | ATS adapter (TruckSim GPS server :31377), Director, first knowledge pack | Companion mentions a POI and low fuel during a real drive without spamming |
| **M3 Memory** (in progress) | Local per-game trip memory: places, jobs and notable moments, session summaries; recall when asked ("have we been here before?") and, rarely, unprompted | "Remember last trip to Reno?" works; spontaneous recall is occasional, never every visit |
| **M4 More games** (ETS2 live 2026-11-16: countries, towns, landmarks; KSP live 2026-11-17, ADR 0006: flight events, navball/target speed, quotes; voice-set alerts ADR 0007; Elite built 2026-11-17, ADR 0010: journal events, written binds, hands; live check and EDSM exploration hints pending) | ETS2 (shares ATS adapter), KSP (kRPC; flight events + rare paraphrased astronaut quotes), then MSFS, Elite (journal + EDSM), Minecraft (see below) | Each has an adapter + knowledge pack |
| **M5 Hands** (built 2026-11-17, ADR 0009; KSP kRPC actions live 2026-11-17, SCS key presses pending) | uinput actions as LLM tools, kRPC actions for KSP, Touch Portal sync (both ways, see below) | Voice command toggles something in-game |
| **M6 Crew** (phases 1-3 built 2026-11-17, ADR 0011; crew turns and banter live in KSP 2026-11-17; Touch Portal part of phase 3 deferred) | Up to three personas at once: launcher picks game, crew, language, action-carrier and banter level into `session.toml`; one speaker at a time; banter scenes; shared memory with speaker attribution. Phases: 1 session + speaker selection, 2 banter, 3 stereo/callbacks/Touch Portal | Ask Frieren a question by name and she answers in her voice; Megumin teases her on a long quiet stretch; Mira handles the gear |

Planned: **how the companion addresses the user.** A name or nickname the user picks (e.g.
"Skipper"), plus a formality level (casual, polite, formal) set globally in `config.toml` /
the GUI and overridable per persona. Each language maps the level to its own register:
Japanese plain form vs です/ます vs 敬語 and honorifics (さん, 様), Spanish tú vs usted, German
du vs Sie. English mostly changes tone.

Nice to have: **the companion as DJ** (in every game; design agreed with the Commander
2026-11-18, ADR pending). The action-carrier is the DJ: picks music for the moment and comments
on it; the rest of the crew react through banter. Play, skip and queue are carrier-only actions.
- **Sources:** a local library on this PC (~4000 tracks: MP3, some FLAC, Apple-bought AAC; some
  old CD rips lack tags) through a local player such as MPD; YouTube Music for discovery, in the
  browser if MPRIS plus opening a track link covers playing a chosen song, else a desktop
  YouTube Music app (YouTube Music only). No Spotify. Apple Music: now-playing only.
- **Now playing:** MPRIS over D-Bus for any player; a track change is an event.
- **Choosing:** about 70% the Commander's library, 20% where the DJ persona's taste (new
  persona field) overlaps, 10% discovery from similar artists (ListenBrainz/MusicBrainz).
  Taste comes from the library, play counts and YouTube Music likes/history (read-only).
  "Occasional" genres (e.g. cumbia, salsa) get a low weight and a cooldown. The game moment
  (night drive, launch, supercruise) sets the mood.
- **Voice feedback:** "skip it, I don't like this one" (dislike stored in memory; a repeat
  dislike of the same artist counts against the artist), "more like this", "who sings this?",
  "something calmer", volume.
- **Audio:** same speakers as the game. Duck the music while anyone speaks; talk over intros or
  between tracks, never over vocals. Stop the music on alerts and urgent events (e.g. an Elite
  interdiction).
- **Tags:** a scan that identifies untagged tracks by audio fingerprint (AcoustID/MusicBrainz)
  and proposes tags for approval, never rewriting files silently.
- Facts about songs (artist, year, album) come from tags and MusicBrainz, never from the model.

Planned (M5, after the first hands): **Touch Portal sync, both directions.** Familiar's actions and
state (alerts, fuel, last action) update panel buttons, and panel buttons trigger Familiar (talk,
status report, set an alert). Needs a Touch Portal plugin connection; see the `touchportal-builder`
skill for panel layouts. Also follow-up: a window-focus check for key presses (KWin scripting on
KDE Wayland) so presses never land in another app.

Done 2026-11-17: **SCS actions as data, with rescan.** The catalogue, default keys, key names,
projections and regions are YAML in `familiar_scs/data/`, overridable in
`~/.config/familiar/data/familiar_scs/`. Bindings reload when the profile file changes.
`familiar scs bindings ats|ets2 [--suggest]` lists keys and unused bound controls; drafts are
written disabled until reviewed. Open: confirm in game that mid-session reload picks up a rebind.

Done 2026-11-17: **outside-owned data moved out of code** (rule in `AGENTS.md`). Shared loader
`familiar.interfaces.data` (packaged YAML + user override). Moved: languages, yes/no words, model
IDs (core); SCS catalogue, keys, projections, regions; KSP resource names, with propellants also
discovered from the engines at runtime (modded fuels work without edits). Open: fly a modded-fuel
stage to confirm discovery live.

Dropped 2026-11-17: **Japanese practice mode.** The Commander chose to practise in SwiftBlade
(the Mac Mini chat front end for CLI agents) and add a way to read its output aloud, instead of
building a tutor into Familiar. The old plan and its pronunciation notes are in
`docs/archive/japanese-practice-mode.md`. ADR 0008's pointer to "the SwiftBlade note, M5" now
lands here.

Planned (M4, later): **Minecraft (Java).** Mod loaders (Forge, NeoForge, Fabric) don't
publish game data, so this needs our own small read-only client mod. The user plays 26.3 in
Prism Launcher on NeoForge (Sodium, Iris, Distant Horizons, Xaero's Minimap). Steps:
1. No mod: tail `logs/latest.log` (vanilla too) for deaths with causes, advancements, chat and
   world joins.
2. A NeoForge client mod serving local JSON, like TruckSim GPS: dimension, biome, position,
   nearby structure, time, weather, light, health/hunger/armour/oxygen, effects, damage source,
   nearby hostiles, held item and durability, advancements, first visits (for memory). Modded
   content appears as registry IDs. Watchables for voice alerts ("warn me under 4 hearts").
3. Later: Fabric via Architectury; actions as tool calls through the mod (Xaero waypoints,
   commands in creative) instead of key injection.
Rebuild per Minecraft version (keep the mod small). It is the first Java code in the project,
so get agy/codex consensus and an ADR first. Bedrock's `/connect` WebSocket gives events with no
mod, but only helps if Bedrock is played.

Done 2026-11-17: **add new languages.** Languages come from `familiar/data/languages.yaml`; the
GUI's Add language control saves to the user override (`POST /api/languages`). Open: check that
each backend (Gemini, ElevenLabs STT/TTS) accepts a newly added code.

Planned: **Mac desk companion with alerts.** Familiar on the Mac as a desk assistant that
speaks up about notifications and events, not games. Core already runs without a game pack;
which alerts matter most is still undecided, so pick sources before building. Steps:
1. macOS push-to-talk on a **mouse button**: the user's Elecom HUGE trackball has spare buttons.
   A `PushToTalk` backend that listens for "other" mouse buttons (pynput/CGEventTap; needs
   Accessibility or Input Monitoring) with the button picked in config or by a "press it now"
   setup, like the joystick. It should consume the press so the button doesn't also act as
   browser back/forward. Check which buttons reach macOS without Elecom's driver; the Fn buttons
   may need mapping there first. A keyboard hotkey is the fallback.
2. A `familiar-desk` pack: a `GameAdapter` registered under `familiar.games` that emits events
   with priorities, so the Director's cooldowns and stale-dropping apply unchanged. Sources:
   - Local webhook (`POST` to localhost) as the core: scripts, cron, Home Assistant, Claude
     Code / agy / codex hooks ("build finished") and CI can all hand Mira something to say.
     No permissions needed.
   - Calendar (Google Calendar API, or EventKit for local calendars), Gmail (filtered), GitHub
     via `gh` (reviews, failed runs), and system state (battery, disk space, network/Tailscale).
3. Manners: respect Focus/Do Not Disturb, stay quiet while another app uses the mic (calls),
   and keep quiet hours.
Not planned: reading other apps' notifications from Notification Center's private database. It
needs Full Disk Access, the format is undocumented and it breaks across macOS versions. If a
permission like that is ever needed, put it in a small signed Swift helper rather than granting
it to Terminal/Python. Get agy/codex consensus and an ADR on the pack design first.

Planned, **test before building: expressive speech with ElevenLabs Eleven v3.** v3 acts out
inline audio tags (`[excited]`, `[sighs]`, `[whispers]`, `[laughs]`), so the LLM could tag each
line with its emotion. Today every line goes to Flash v2.5 with default voice settings, so
emotion comes only from wording and punctuation. v3 is slower than Flash; ElevenLabs recommends
Flash for live agents. First run a test: time tagged v3 lines against Flash on the user's
connection and listen in en/es/ja. Only build if the delay is acceptable, and then possibly
only for event remarks, keeping Flash for alert callouts. A build would also strip tags from
transcripts and before the Gemini TTS fallback.

Later options: Gemini Live speech-to-speech `Conversation` backend, local backends (whisper.cpp
Vulkan, Kokoro, Ollama), note taker to an Obsidian vault, standard SCS telemetry
plugin for users without the TruckSim GPS port.

Before publishing to PyPI: rename the distributions (`familiar` is taken there by an unrelated
project), e.g. `familiar-companion` and `familiar-companion-scs`.

SCS events added 2026-11-16 (ATS and ETS2): tolls, inferred traffic jams, hard braking and
trailer collisions, after-rest check-in, time of day from sun height, rain hint from wipers.
The telemetry has no traffic, AI-vehicle or weather data; these are inferred from the player's
own truck. Open checks: solar-noon hour (`SOLAR_NOON_HOUR`), UK projection branch, Iceland map
(not in the gazetteer or landmarks yet), jam thresholds.
