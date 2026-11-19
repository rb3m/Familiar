# 0012: The companion as DJ: a music service beside the game, YouTube Music via Pear Desktop first

Date: 2026-11-18
Status: Accepted

## Context

The Commander wants the action-carrier to act as DJ in every game and in desk mode: pick music
for the moment, comment on it now and then, and take voice commands (skip, "I don't like this",
more like this, who sings this, something calmer, volume). The rest of the crew react through
banter. Music shares the speakers with the game and the voices, so it must duck when anyone
speaks, never have a remark land on vocals, and stop for critical events. The design direction
is in `docs/roadmap.md` (DJ entry). The local library (~4000 files, later counted at ~12,000) was still being copied, so
phase 1 uses YouTube Music; the Commander has Premium (no ads) and accepted a desktop app.

Pear Desktop (formerly th-ch/youtube-music, x86_64 rpm 3.12.1) has an `api-server` plugin:
REST on a configurable host/port (default `0.0.0.0:26538`, set to `127.0.0.1`), a JWT issued
once after an in-app Allow dialog, search, queue add by video ID (after current / at end),
next/previous/play/pause, song info, volume, like/dislike, queue read and a websocket. Live
tests on 2026-11-18 found:
- Enabling the plugin needs a full app restart: the HTTP server starts at once, but its in-page
  half loads only at launch.
- The page handlers register only after the player has loaded once (first play in a launch).
- When either condition is unmet, search and queue reads hang forever and queue adds are
  silently dropped (HTTP 204). No error, no timeout.
- After a restart and one play: song info, volume, queue read (51 items), search (0.7 s) and
  queue add after the current track all work.
`ytmusicapi` (unauthenticated) searched in 1.2 s with parsed results (video ID, title, artists,
album, duration), independent of the app's page state.

## Options

1. Placement: new `familiar-music` package with a companion-service entry point; inside core;
   as a `GameAdapter` (impossible: one adapter per session, music must run beside it).
2. Player contract: one `MusicPlayer` protocol (Pear now, MPD later) vs per-backend code.
3. Now-playing source: Pear's websocket/HTTP vs MPRIS (generic, but no video ID, search or likes).
4. Ducking: the player's volume API vs PipeWire per-stream volume.
5. Speaking windows: guessed intro/outro windows from elapsed time; synced-lyrics timestamps;
   audio analysis; only at track changes and when asked.
6. Selection: the DJ queues every track itself vs seed + radio continuation.
7. Search: Pear's search endpoint vs `ytmusicapi`.

## Decision

- **Placement.** New package `familiar-music`. Core grows a small, additive companion-service
  contract in `familiar.interfaces` and a `familiar.services` entry-point group, loaded beside
  the game adapter (and in desk mode) when `[music]` is enabled. A service supplies `events()`
  for the Director, `tools()` for the ToolRegistry, and a speech-state hook fed from
  `VoiceSession._busy` (`user`, `speaking`, `critical`). Additive, so `PLUGIN_API_VERSION` gets a
  minor bump only.
- **One `MusicPlayer` protocol**: search, play_now, enqueue, skip, pause/resume, now_playing,
  get/set volume, like/dislike, plus a capability set (MPD will lack likes). Pear is the first
  backend. Host, port, endpoints and timeouts live in `familiar_music/data/*.yaml` with the usual
  user override; the JWT is issued at runtime and kept in the user state dir (mode 0600), never
  in config or the repo.
- **Now-playing from Pear** (song info polled at a low rate, websocket later if needed); MPRIS is
  cut from phase 1 (a possible generic backend later).
- **Ducking via the player's volume**, one coordinator for every speaker, reference-counted.
  The user's chosen base volume is separate from the duck level; volume is restored on
  cancellation, errors and shutdown. Critical events pause music; it resumes only if Familiar
  paused it and the user hasn't since.
- **Speaking windows: no guessing.** Replies to the player duck. Unprompted DJ remarks happen
  only at a track change and pause the new track until the remark ends, so a remark can never
  land on vocals. Track-change remarks are rare (Director cooldown plus a music-specific one).
  Synced lyrics and audio analysis are cut.
- **Seed and radio.** The DJ turns the moment and mood into a search, plays the pick now (add
  after current, then next), and keeps a short queue topped up from that track's radio, filtered
  by stored dislikes. "Calmer", "more like this" and a dislike re-seed. Dislikes go to the trip
  memory SQLite (track; a repeat for the same artist counts against the artist) and to Pear.
  *Amended 2026-11-19 after the Elite test:* a genre or mood ("disco") matched song titles, so
  `play_music` now takes `kind`. Genres start a shuffled batch from a curated YouTube Music
  playlist (community ones as fallback), keeping the rest as a reserve before radio; artists and
  songs stay seed + radio. A new pick first clears Pear's upcoming queue (radio used to land
  behind the old list), waits for Pear's asynchronous queue add before skipping, and avoids
  replaying recent tracks and playlists.
- **Search with `ytmusicapi`, unauthenticated**; Pear does playback and control. It avoids the
  page-state hang and gives parsed results. Account login (likes/history for taste) is a later
  phase.
- **Health check with short timeouts.** Every Pear call has a timeout. On connect, Familiar
  checks song info; empty or timing out means "press play once in YouTube Music" (or restart it
  after enabling the plugin), said once, not retried in a loop. A missing token runs the
  one-time Allow handshake.

## Consequences

Familiar depends on a separate app being open, restarted after enabling the plugin and played
once per launch; the health check turns that into one clear sentence instead of silent hangs.
`ytmusicapi` is an unofficial library and can break when YouTube changes; it sits behind the
`MusicPlayer`/search seam so it can be swapped. Pausing for unprompted remarks costs a few
seconds of each commented track, a deliberate trade for never talking over vocals. The local
library (MPD), taste weighting (70/20/10), fingerprint tagging and YouTube Music history import
stay in later phases of the roadmap entry.

## Who agreed

- agy, Codex (gpt-6.1-sol, workhorse tier), Grok (grok-4.7, high) all picked: separate
  `familiar-music` package with a service contract; one `MusicPlayer` protocol with Pear as the
  single state source; ducking via player volume triggered by speech start/stop; no guessed
  speaking windows; seed + radio; account login and taste later. Grok and Codex: critical
  events pause, not duck; separate base volume from duck level. Codex: pause music for
  unprompted remarks, and validate Pear's real behaviour first (borne out by the live tests).
- Coordinator (Claude): `ytmusicapi` for search after the Pear hang, the health check, token
  storage.
- Commander: YouTube Music first (Premium), Pear Desktop accepted, plugin bound to 127.0.0.1.
