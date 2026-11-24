# Voice server: contract and layout (ADR 0029)

A local TTS server for the LAN. A tiny always-on **supervisor** (launchd on the M1 Mac Mini)
loads one model **worker** process while any Familiar holds a **lease**, and ends the worker
when the last lease ends or expires. Familiar's `local` TTS backend is an HTTP client for it,
first in the chain local → ElevenLabs → Gemini.

## Layout
- `voice-server/`: its own uv project (`familiar-voice-server`), **not** a workspace member.
  MLX and mlx-audio stay out of the Familiar workspace. Apple Silicon only (the worker).
  - `src/familiar_voice_server/supervisor.py`: HTTP server, leases, worker lifecycle, proxy.
    Never imports MLX.
  - `src/familiar_voice_server/worker.py`: loads the model and voices, synthesizes one request
    at a time, streams PCM. The only module that imports mlx/mlx-audio.
  - `config.example.yaml`: server settings and voices; the live copy is
    `~/.config/familiar-voice/config.yaml`, with reference clips beside it.
  - `packaging/`: launchd plist template and `install.sh`.
- `packages/familiar/src/familiar/backends/local/`: the `local` TTS backend (HTTP client only).

## Server config (`config.yaml`)
```yaml
listen: {host: 0.0.0.0, port: 8765}
token_env: FAMILIAR_VOICE_TOKEN      # shared bearer token; required
lease: {ttl_s: 30, grace_s: 60}      # expiry without renewal; idle time before unloading
worker: {stop_timeout_s: 10}         # SIGTERM, then SIGKILL
model:
  id: mlx-community/Qwen3-TTS-12Hz-0.6B-Base-6bit
  streaming_interval_s: 0.32
voices:                              # voice id -> how to speak it (paths relative to the file)
  sarah:    {mode: clone, ref_audio: voices/sarah-short.wav, ref_text: "Where to Push Back..."}
  marichka: {mode: clone, ref_audio: voices/marichka-short.wav, ref_text: "..."}
  sakuya:   {mode: clone, ref_audio: voices/sakuya-short.wav, ref_text: "..."}
languages: {en: english, es: spanish, ja: japanese}   # Familiar code -> model language
```
`mode: design` (with `description`) is reserved for a VoiceDesign model on a bigger machine.
The worker rejects a voice whose mode its model can't do.

## HTTP API
Every request needs `Authorization: Bearer <token>`; otherwise **401**. JSON unless noted.

| Method and path | Body | Answer |
|---|---|---|
| `POST /v1/leases` | `{"client": "mac-desk"}` | **201** `{"lease_id", "ttl_s", "state"}`; starts the worker if it isn't running |
| `PUT /v1/leases/{id}` | none | **200** `{"ttl_s", "state"}`; **404** if expired or unknown (client takes a new lease) |
| `DELETE /v1/leases/{id}` | none | **204** (also for unknown ids) |
| `GET /v1/status` | none | **200** `{"state", "leases", "model", "voices", "busy"}` |
| `POST /v1/speech` | `{"text", "voice", "language"}` | **200** streamed `audio/L16; rate=24000; channels=1` (raw little-endian PCM16, body chunks as generated) |

- **States**: `idle` (no worker) → `loading` → `ready` → `stopping` → `idle`; `failed` (worker
  crashed or wouldn't load; the next lease retries).
- **`POST /v1/speech` errors** (all JSON `{"error", "state"}`):
  - **503** when not `ready` (`loading`, `idle`…), or when the worker is **busy** with another
    request. Requests are never queued.
  - **400** for an unknown voice or language, or empty text.
  - **500** if synthesis fails before any audio. A failure after audio has started just ends
    the stream early, and the client treats a short stream as a stall.
- **Lifecycle**:
  - A worker starts on the first live lease. When no unexpired leases remain, the supervisor
    waits `grace_s`, lets any in-flight request finish, then sends SIGTERM to the worker,
    and SIGKILL after `stop_timeout_s`. A lease taken during the grace period cancels the stop.
  - Supervisor and worker talk over a private local channel of the implementer's choice
    (e.g. a localhost port or Unix socket chosen at spawn). The worker is never exposed to the LAN.
  - The supervisor's memory stays small (no MLX imports).

## Client (`local` backend) behaviour
Settings live in `[tts.local]`: `url`, `token_env` (default `FAMILIAR_VOICE_TOKEN`),
`client_name`, `first_audio_deadline_ms` (1000), `stall_margin_ms` (300), `renew_s` (10),
`park_after_misses` (3 within `park_window_s` 120), `park_s` (300), `stall_park_s` (20),
`timeout_seconds`.
- **Lease**: taken in the background at construction (the backend is built inside the running
  loop) and renewed every `renew_s`. On 404 it takes a new lease; on network errors it keeps
  retrying quietly. `aclose()` cancels renewal and sends `DELETE`.
- **Synthesis**:
  - Before the server reports `ready`, raise `BackendError(retryable=True)` straight away,
    so the 4–18 s warm-up is spoken by the fallback. Readiness comes from lease/renew/status
    answers, so no request is spent finding out.
  - **First-audio deadline**: if no audio arrives within `first_audio_deadline_ms`, cancel the
    request and raise a retryable error.
  - **Stall**: after the first chunk, estimate queued audio as (audio received) − (time since
    the first chunk). If it falls below −`stall_margin_ms` (playback has caught up and is
    waiting), keep yielding the rest of this sentence but **park** the backend for the rest of
    the turn: `stall_park_s` (20 s). The stall also counts as a miss towards `park_s`.
  - **Parking**: deadline misses and stalls count as misses. `park_after_misses` misses within
    `park_window_s` park it for `park_s`. While parked, synthesize raises retryable immediately.
  - Errors map to `BackendError`: 401 → not retryable; 400 → not retryable; 503, 5xx and
    network errors → retryable.
- **Visibility**: the first lease failure logs a warning with the reason, and recovery logs
  once. With `FAMILIAR_VOICE_MARKERS=1` (the GUI sets it), the voice loop prints
  `[voice] <backend>` after each spoken sentence. `FallbackTTS.last_backend` names the backend
  that produced it. The GUI hides these lines and colours the sentence. Its `GET /api/local-voice`
  proxies `/v1/status` (cached 10 s) for the status card, with extra states `unreachable`,
  `unauthorized` and `unconfigured`.
- **macOS clients** reach the server through `packaging/macos/install-voice-tunnel.sh`: a
  launchd daemon (running as the user) that keeps `ssh -L 127.0.0.1:8766 -> server:8765` open,
  because Local Network privacy blocks Familiar.app (ADR 0030). `url` is then
  `http://127.0.0.1:8766`.
- **Capabilities**: `{"streaming"}`, without `audio_tags` (ADR 0020), so tags are stripped.
- **Voices**: personas name a server voice id per language,
  e.g. `voices: {local: marichka, elevenlabs: VRgX...}`.
