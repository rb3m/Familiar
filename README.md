# Familiar

A voice companion with a personality for sim games and the desk. Hold a push-to-talk button,
talk, and your familiar answers out loud. In games it also speaks up on its own when something
worth mentioning happens: a famous landmark near your truck, fuel running low, a long night drive.

- **Push-to-talk voice loop** with barge-in: press the button while it talks to interrupt.
- **Personas as YAML**: prompt, style rules, chattiness, voices per language, and a truthfulness
  mode (`strict` never invents facts; `playful` may exaggerate in character). The core ships one
  original persona, **Mira**, a small lantern-spirit familiar.
- **Plugins for everything**: LLM, speech-to-text, text-to-speech, push-to-talk devices, game
  adapters and CLI commands are discovered through Python entry points. Gemini and ElevenLabs
  are built in; adding another provider is a small class (see `docs/plugins.md`).
- **Game packs** install separately. `familiar-scs` adds American Truck Simulator and
  Euro Truck Simulator 2: live telemetry, a hand-checked landmark pack and town lookup.
  `familiar-ksp` adds Kerbal Space Program flight telemetry, mission events and resource warnings
  through the kRPC mod (enable auto-start servers and auto-accept clients).
- **Launcher GUI** (`familiar gui`): pick the game and persona, watch the conversation, and
  create or edit personas, with optional AI drafting and voice previews.
- **Voice-set telemetry alerts** in KSP, ATS and ETS2: ask “tell me at 4,000 metres”,
  “warn me when we're 50 km out” or “tell me when fuel drops under 25%”. Alerts fire once,
  last for the session and can be listed or cancelled by voice. Configure `[alerts]` in TOML.
- **Hands** (Linux): ask Mira to act in game. ATS/ETS2 by pressing your own key bindings
  (read from your profile): beacon, hazards, lights, wipers, engine, parking brake, cruise
  control and more. KSP through kRPC: stage, SAS modes, RCS, gear, throttle, time warp. Risky
  actions (engine off, staging, warp, trailer) wait for a spoken "yes". Configure `[hands]`.
  SCS bindings rescan automatically before presses; inspect them with `familiar scs bindings ats|ets2`.
- **Trip memory**: remembers earlier places and trip events across personas, answers questions
  about previous visits, and occasionally recalls a distinctive moment when you return.
  Facts stay in a local SQLite database; `[memory]` controls storage and recall frequency.
- **Elite Dangerous hands**: gear, lights, cargo scoop, ship/suit night vision and power
  distribution use your active keyboard bindings with status read-back; experimental docking
  requests use a configurable Contacts sequence and report the journal outcome without retries.
- **Elite Dangerous exploration**: on request, two or three interesting systems within jump
  range and the least-reported directions to explore, from EDSM's public data (cached, read-only).

Status: early and personal, but working. Linux (KDE Wayland) is the daily driver; macOS on Apple
Silicon is the next target. See `docs/roadmap.md`.

## Install

Familiar needs Python 3.12 and [uv](https://docs.astral.sh/uv/) (or pip).

```bash
git clone https://github.com/rb3m/Familiar.git
cd Familiar
uv sync --all-packages        # core + ATS/ETS2 and KSP packs
cp config.example.toml config.toml
```

Or install with pip, core only or core plus the truck pack:

```bash
REPO=git+https://github.com/rb3m/Familiar.git
pip install "familiar @ $REPO#subdirectory=packages/familiar"
pip install "familiar @ $REPO#subdirectory=packages/familiar" \
            "familiar-scs @ $REPO#subdirectory=packages/familiar-scs"
```

Always name the core explicitly like this: the name `familiar` on PyPI belongs to an unrelated
project, so a bare `familiar-scs` install would pull the wrong package.

### Keys

Secrets live in environment variables; `config.toml` only names them.
Language, confirmation-word and model defaults can be overridden in
`~/.config/familiar/data/familiar/*.yaml` (`FAMILIAR_DATA_DIR` changes the root); write only keys you change.

| Service | Variable (default) | Used for |
|---|---|---|
| Gemini API (free tier works) | `GEMINI_API_KEY` | LLM, fallback STT/TTS |
| ElevenLabs (free tier works) | `ELEVENLABS_API_KEY` | Fast STT and TTS (needs Text to Speech and Speech to Text access) |

Without ElevenLabs, point `[stt]` and `[tts]` at `gemini` in `config.toml`.

## Use

```bash
uv run familiar gui                                # launcher window
uv run familiar chat                               # text chat in the terminal
uv run familiar voice --game ats --persona mira    # voice, with ATS telemetry
uv run familiar ptt-setup                          # bind a joystick/HOTAS button to push-to-talk
uv run familiar plugins                            # list installed backends and game packs
```

On Linux, `packaging/linux/install.sh` adds Familiar to the applications menu.

ATS/ETS2 telemetry comes from a local TruckSim GPS telemetry server (default
`http://localhost:31377`); see `[games.ats]` in `config.example.toml`.

For KSP, install the kRPC mod and enable **Auto-start servers** and **Auto-accept new clients**.
kRPC 0.7 ships with no server defined: in the Space Center open the kRPC window, **Add server**
(Protobuf over TCP, localhost, RPC port 50000, stream port 50001) and start it once; it is saved
and auto-started afterwards. Check with `uv run familiar ksp-probe`.
Run `familiar ksp-probe` to inspect a snapshot or `familiar ksp-record flight.jsonl` to record one.
Select KSP with `familiar voice --game ksp --persona mira`. Live flight verification is pending.

Memory is enabled by default at `$XDG_DATA_HOME/familiar/memory.db`, falling back to
`~/.local/share/familiar/memory.db`. Set `[memory] enabled = false` to disable it, or `path`
to choose another database. Retrieval is local; clean shutdown asks the configured LLM for a
short factual summary of sessions with at least four user/assistant utterances (15-second limit).

### Your own personas

Create them in the GUI (Personas tab) or copy
`packages/familiar/src/familiar/personas/mira.yaml` to `~/.config/familiar/personas/<id>.yaml`.
Personas there override built-in ones with the same id. Please keep personas of copyrighted
characters in your own folder rather than contributing them here.

## Documentation

- `docs/architecture.md`: modules, interfaces and the Director's speaking rules.
- `docs/plugins.md`: adding an LLM/STT/TTS provider, a command, or a game pack.
- `docs/knowledge-packs.md`: landmark packs and the `familiar landmarks` workflow.
- `docs/gui-api.md`: the launcher's local HTTP API.
- `docs/decisions/`: architecture decision records.

## Development

```bash
uv sync --all-packages
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Place data in the SCS pack comes from the US Census Gazetteer (public domain); landmark facts
were checked against Wikipedia articles.

## License

MIT, see `LICENSE`.
