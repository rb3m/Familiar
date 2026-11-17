# GUI launcher API (contract between `packages/familiar/src/familiar/gui/server.py` and `static/`)

`familiar gui [--port 8765] [--no-browser] [--config PATH]` starts an aiohttp server bound to
127.0.0.1 only, generates a random per-launch token, and opens `http://127.0.0.1:PORT/?token=T`
(an app-mode window via chromium/google-chrome/brave/msedge `--app=URL` when available, else the
default browser). Static files (`index.html`, `app.js`, `style.css`, `familiar.svg`) are served
from `/` without a token.

## Auth
Every `/api/*` request must carry header `X-Familiar-Token: T` (EventSource: query `?token=T`).
Requests with a wrong/missing token, or an `Origin` header that is not this server's own origin,
get `403`. The frontend reads the token from `location.search` once and keeps it in memory.

## Errors
Non-2xx responses have JSON `{"error": "human readable message"}`.

## Endpoints
- `GET /api/state` → `{"games": [{"id": "ats", "name": "American Truck Simulator", "icon": "🚛"}, ...],
  "personas": [PersonaSummary], "languages": [{"id": "en", "name": "English"}, ...],
  "defaults": {"game": "ats" | null, "persona": "mira", "language": "en"},
  "session": Session, "quota": {"used": 120, "limit": 10000} | null}`
  - `PersonaSummary` = `{"id", "display_name", "source", "truthfulness", "chattiness",
    "languages": ["en", "ja"], "builtin": bool, "overrides_builtin": bool}`; `builtin` = shipped
    in `packages/familiar/src/familiar/personas/` (read-only; editing saves a user copy with the same id, which then
    takes precedence); `overrides_builtin` = a user copy shadowing a shipped persona (DELETE
    restores the original, so the UI labels it "Reset to built-in").
  - `Session` = `{"running": bool, "game": str | null, "persona": str | null, "language": str | null,
    "started_at": ISO-8601 | null, "exit_code": int | null}`
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
  and `"overrides_builtin": bool`.
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
- `POST /api/session/start` body `{"game": "ats" | null, "persona": "mira", "language": "en"}`
  → `200` Session; `409` if one is already running.
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
