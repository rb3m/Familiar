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
config data; the checkpoint must be available to mlx-audio on the M1. Restart after
config changes. Reinstalling after changing the token file or `token_env` updates
the plist; launchd may need a `launchctl bootout gui/$(id -u)/com.familiar.voice-server`
followed by the installer to reload changed environment variables.

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
