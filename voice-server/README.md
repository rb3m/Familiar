# Familiar voice server

A small authenticated LAN TTS supervisor for Apple Silicon. A separate Qwen3-TTS
worker loads on the first lease and exits after the last lease expires plus a grace
period, releasing model memory. One sentence runs at a time; busy/loading requests
get 503 immediately. Audio streams as mono 24 kHz little-endian PCM16.

On the M1 Mac Mini, install `uv`, then from this directory:

```sh
mkdir -p ~/.config/familiar-voice
# Put your shared bearer token in ~/.config/familiar-voice/token (one line).
chmod 600 ~/.config/familiar-voice/token
./packaging/install.sh
```

The installer syncs the worker dependencies, copies `config.example.yaml` if needed,
and installs/starts the current checkout as a user LaunchAgent. Run it again to
restart. Logs: `~/Library/Logs/familiar-voice-server.log`.

Edit `~/.config/familiar-voice/config.yaml`. Put the reference WAVs at
`voices/{sarah,marichka,sakuya}-short.wav` relative to that file, and set each
`ref_text` to the clip's exact transcript. Clone voices require a Base checkpoint;
`design` voices require a VoiceDesign checkpoint and `description`. Model ids are
config data; mlx-audio downloads the checkpoint on first load (about 80 s; later loads
take about 10 s). Before reporting `ready`, the worker speaks a short warm-up phrase in
each voice, so the first real sentence is not a cold start. Rerun the installer after
changing the config, the token file or `token_env`. It unloads and reloads the
LaunchAgent so the changes take effect, using the `gui/` domain when someone is logged in
and `user/` otherwise.

If the worker crashes, the state becomes `failed`. The next new lease, or a lease renewal
at least 30 s after the crash, starts it again. Check with
`curl -H "Authorization: Bearer $FAMILIAR_VOICE_TOKEN" http://<host>:8765/v1/status`.

Short reference clips (8–10 s) are recommended. With a 20 s clip, the M1 synthesizes
slower than real time (see `docs/local-tts.md`).

The supervisor reads the token from the environment variable named by `token_env`
(default `FAMILIAR_VOICE_TOKEN`). For a foreground run:

```sh
export FAMILIAR_VOICE_TOKEN="$(cat ~/.config/familiar-voice/token)"
uv run familiar-voice-server --config ~/.config/familiar-voice/config.yaml
curl -H "Authorization: Bearer $FAMILIAR_VOICE_TOKEN" http://localhost:8765/v1/status
```

Every public endpoint requires that bearer token. `POST /v1/leases` with
`{"client":"mac-desk"}` returns a UUID, TTL and state. Renew with
`PUT /v1/leases/{id}`, release with `DELETE /v1/leases/{id}`. Once `ready`, send
`POST /v1/speech` with `{"text":"Hello","voice":"sarah","language":"en"}`.
The worker listens only on loopback; the supervisor defaults to port 8765 on all
interfaces. Only expose it to your trusted LAN.

Offline tests (fake engine/process; no GPU or downloads): `uv run --offline pytest -q`.
