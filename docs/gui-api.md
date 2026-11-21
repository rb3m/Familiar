# GUI launcher API (contract between `packages/familiar/src/familiar/gui/server.py` and `static/`)

`familiar gui [--port 8765] [--no-browser] [--config PATH] [--session PATH]` starts an aiohttp
server bound to 127.0.0.1 only, generates a random per-launch token, and opens
`http://127.0.0.1:PORT/?token=T` (an app-mode window via chromium/google-chrome/brave/msedge
`--app=URL` when available, else the default browser). Static files (`index.html`, `app.js`,
`style.css`, `familiar.svg`) are served from `/` without a token. `--session` chooses the
session file the launcher edits and starts with (default `~/.config/familiar/session.toml`).

## Auth
Every `/api/*` request must carry header `X-Familiar-Token: T` (EventSource: query `?token=T`).
Requests with a wrong/missing token, or an `Origin` header that is not this server's own origin,
get `403`. The frontend reads the token from `location.search` once and keeps it in memory.

## Errors
Non-2xx responses have JSON `{"error": "human readable message"}`.

## Endpoints
- `GET /api/ptt` → `{"backend", "settings", "label", "detectable": bool,
  "kinds": ["mouse", "key"], "kind": "mouse" | "key" | "joystick" | null}`.
  Labels include `Mouse button 4`, `BTN_TRIGGER on Thrustmaster`, `Key Ctrl+F12`, and
  `Terminal (Enter)`. `kinds` lists platform detection options (macOS: mouse/key;
  Linux: joystick; others: empty); `kind` is the first option or null for older clients.
- `POST /api/ptt/detect` body `{"timeout": 30, "kind": "key"}` (optional, positive seconds, at most 60)
  → waits for one press and returns the same shape as `GET /api/ptt` with proposed settings,
  without saving. `409` while a session or detection is running; `408` on timeout.
  Optional `kind` defaults to the first platform kind; unsupported kinds return `400`.
  Key detection consumes only F1-F20/keypad keys and passes other keys through; it records
  held modifiers, e.g. `{"key": 111, "modifiers": ["control"]}`.
  Mouse detection consumes spare buttons to prevent back/forward navigation.
- `PUT /api/ptt` body `{"backend", "settings"}` → validates and saves to the server's config
  path, reloads its PTT config, and returns the same shape as GET. Mouse requires an integer
  `button >= 2` and optional boolean `consume` (default true). Key requires an integer
  `key` in the F1-F20/keypad virtual-keycode mapping, optional `modifiers` (a list of
  `control`, `option`, `shift`, `command`; matched exactly, so plain F12 stays free when Ctrl+F12
  is assigned) and optional boolean `consume` (default true), with no other settings; evdev requires nonempty
  `device` and `button` strings; terminal accepts empty settings. `400` on invalid input,
  `409` while a session or detection runs. Session start is also blocked during detection.
  All PTT endpoints use the token and origin checks described above.
- `GET /api/state` → `{"games": [{"id": "ats", "name": "American Truck Simulator", "icon": "🚛"}, ...],
  "personas": [PersonaSummary], "languages": [{"id": "en", "name": "English"}, ...],
  "defaults": {"game": "ats" | null, "persona": "mira", "language": "en"},
  "session": Session, "quota": {"used": 120, "limit": 10000} | null}`
  - `PersonaSummary` = `{"id", "display_name", "source", "truthfulness", "chattiness",
    "languages": ["en", "ja"], "roles": {"elite": "the ship's onboard AI", ...},
    "aliases": {"en": ["Mira"], ...}, "builtin": bool, "overrides_builtin": bool}`;
    `roles` maps game ids to the persona's role in that game (shown in the crew picker);
    `aliases` maps languages to the names the crew answers to. `builtin` = shipped
    in `packages/familiar/src/familiar/personas/` (read-only; editing saves a user copy with the same id, which then
    takes precedence); `overrides_builtin` = a user copy shadowing a shipped persona (DELETE
    restores the original, so the UI labels it "Reset to built-in").
  - `Session` = `{"running": bool, "game": str | null, "persona": str | null, "language": str | null,
    "crew": [str, ...] | null, "carrier": str | null, "dj": str | null, "banter": int | null,
    "started_at": ISO-8601 | null, "exit_code": int | null}`. `persona` mirrors `carrier` for
    older clients; a legacy start reports a crew of one with `banter` null.
  - Game names: ats "American Truck Simulator", ets2 "Euro Truck Simulator 2"; unknown ids
    title-cased. The list comes from installed `familiar.games` plugins.
- `GET /api/languages` → `[{"id": "en", "name": "English", "native": "English"}, ...]`
  from packaged data plus user overrides, read afresh. `/api/state` includes the same catalog.
- `POST /api/languages` body `{"code": "pt-BR", "name": "Portuguese", "native": "Português" (optional)}`
  → `200 {"id": "pt-BR", "name": "Portuguese", "native": "Português"}`; native defaults to name.
  Codes use 2–3 letters with an optional uppercase two-letter or three-digit region. Names must
  be non-empty strings. Invalid input returns `400`; the usual token and origin checks apply.
  Saves only user changes to `~/.config/familiar/data/familiar/languages.yaml` (or
  `$FAMILIAR_DATA_DIR/familiar/languages.yaml`), preserving other overrides. Enable the language
  and configure its voices in each persona that will use it.
- `GET /api/personas/{id}` → full persona as JSON (same keys as the YAML) + `"builtin": bool`
  and `"overrides_builtin": bool`. This includes optional `tastes`, e.g.
  `{"music": ["70s city pop"]}`; the editor's "Music taste" box edits `tastes.music` and keeps
  other topics.
- `PUT /api/personas/{id}` body = full persona JSON → validated with the persona loader rules;
  saved as YAML in the user personas dir; `200` → PersonaSummary; `400` on validation error.
  The id in the URL wins over any id in the body. Ids: `^[a-z0-9][a-z0-9_-]{0,39}$`.
- `DELETE /api/personas/{id}` → user personas only (`400` for a builtin without a user copy);
  deleting a user copy of a builtin restores the builtin.
- `POST /api/personas/draft` body `{"character": "Sherlock Holmes", "source": "Arthur Conan Doyle"
  (optional), "notes": "..." (optional)}` → the LLM drafts a persona (JSON in the same shape,
  validated, NOT saved) → `200` persona JSON; `502` with error if the LLM output is unusable.
  Draft voices: Gemini names outside the known list become "Kore"; ElevenLabs is always null
  (the LLM cannot know the user's voice library).
- `GET /api/voices` → `{"gemini": ["Achernar", "Kore", ...30 names], "elevenlabs":
  [{"id", "name", "labels": {...}}] , "elevenlabs_error": str | null}`.
- `POST /api/voices/test` body `{"backend": "elevenlabs" | "gemini", "voice": "id-or-name",
  "language": "en", "text": "optional sample"}` → speaks the sample on this machine;
  `200 {"ok": true}`.
- `GET /api/session/settings` → the launcher's saved selections. With no session file: derived
  defaults (`200 {"version": 3, "game", "crew": [config persona], "language", "carrier",
  "dj": [config persona], "banter": 2, "stereo": false, "seats": {"<carrier>": 0.0}, "problems": []}`). With a valid
  file: its values plus `"problems": []` (version 1 files load with `"stereo": false` and
  default seats; files without `dj` report the carrier). With an
  invalid file: `200` with the raw values that parsed plus `"problems": [...]` (schema and
  persona problems, reported together) so the UI can show and repair them. Never writes.
- `PUT /api/session/settings` body `{"version": 3, "game": "ats" | null, "crew": ["mira", ...1-3
  unique persona ids], "language": "en", "carrier": "<a crew member>", "dj": "<a crew member>"
  (optional, defaults to the carrier), "banter": 0-10,
  "stereo": bool, "seats": {"<crew id>": pan}}` — `stereo` (optional, default `false`) turns on
  spatial placement; `seats` (optional) maps crew ids to a pan from -1.0 (left) to 1.0 (right),
  with missing members defaulted (carrier 0.0, others alternating +0.7, -0.7 in roster order) and
  non-crew keys rejected →
  validated with the session-settings rules (all problems at once, including personas without a
  voice for the language) → `400 {"error", "problems": [...]}` or an atomic save (always written
  as version 2) → `200` with the saved settings.
- `POST /api/session/start` with no body (or `{}`): load and validate the session file; `400
  {"error", "problems": [...]}` if missing or invalid; otherwise spawn `familiar voice
  --session PATH [--config ...]` — the file is the single source, so no `--persona`/`--game`/
  `--language` flags are passed → `200` Session; `409` if one is already running. The legacy
  body `{"game": "ats" | null, "persona": "mira", "language": "en"}` still works: it starts a
  crew of one via CLI flags and writes nothing.
- `POST /api/session/stop` → `200` Session (graceful SIGINT, then terminate after 20 s and kill
  after another 2 s; the grace period includes the trip summary's 15-second deadline).
- `POST /api/shutdown` → stops any session and exits the server (`200` then exit).
- `GET /api/events?token=T` (Server-Sent Events). Event types:
  - `line`: `{"stream": "stdout" | "stderr", "text": "...", "ts": ISO-8601}`, one per output line
    of the session. Lines look like `You: ...`, `Mira: ...`, `Mira (event: fuel_low): ...`,
    `listening…`, `Voice error: ...`, or other diagnostics.
  - `session`: Session (sent on connect and whenever it changes).
  - `ping`: `{}` every 15 s.
  On connect the server first replays the last 200 `line` events of the current/last session.

## Lifecycle
The server owns at most one session subprocess (`python -m familiar voice ...`, unbuffered). It
survives page reloads. If no EventSource client has been connected for 10 minutes and no session
is running, the server exits. Closing the server always stops the session.
