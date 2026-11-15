"""Command-line entry points for backend discovery and interactive chat."""

import argparse
import asyncio
import signal
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from dataclasses import dataclass
from typing import Any

from familiar.audio.player import Player
from familiar.audio.recorder import Recorder
from familiar.config import AppConfig, BackendConfig, load_config
from familiar.conversation import Conversation
from familiar.director import Director, feed_events
from familiar.fallback import FallbackSTT, FallbackTTS, close_backend
from familiar.interfaces import BackendError, ConfigError, GameAdapter, GameEvent, PluginError
from familiar.persona import Persona, load_persona
from familiar.plugins import GROUPS, available, load
from familiar.transcript import TranscriptLog
from familiar.voice import VoiceSession

_ERRORS = (ConfigError, PluginError, BackendError)


async def _reply(
    conversation: Conversation,
    text: str = "",
    director: Director | None = None,
    event: GameEvent | None = None,
) -> None:
    label = conversation.persona.display_name
    if event is not None:
        label += f" (event: {event.kind})"
    source = conversation.ask(text) if event is None else conversation.interject(event)
    said = False
    if director is not None:
        director.set_busy("speaking", True)
    try:
        async with aclosing(source) as sentences:
            async for sentence in sentences:
                print(f"{label}: {sentence}", flush=True)
                said = True
    finally:
        if director is not None:
            if said:
                director.notify_spoke()
            director.set_busy("speaking", False)


async def _cancellable_reply(
    conversation: Conversation, text: str, director: Director | None = None
) -> None:
    task = asyncio.create_task(_reply(conversation, text, director))
    loop = asyncio.get_running_loop()
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(task.cancel))
    try:
        await task
    except asyncio.CancelledError:
        print("\nReply interrupted.")
    except _ERRORS as exc:
        print(f"\nError: {exc}", file=sys.stderr)
    finally:
        signal.signal(signal.SIGINT, previous)


@dataclass
class _Runtime:
    config: AppConfig
    persona: Persona
    language: str
    stack: AsyncExitStack

    def backend(self, kind: str, spec: BackendConfig) -> Any:
        primary = load(kind, spec.backend, spec.settings)
        self.stack.push_async_callback(close_backend, primary)
        if spec.fallback is None:
            return primary
        secondary = self.backend(kind, spec.fallback)
        if kind == "stt":
            return FallbackSTT(primary, secondary)
        return FallbackTTS(primary, secondary, lambda name: self.persona.voice(name, self.language))

    def conversation(
        self, args: argparse.Namespace
    ) -> tuple[Conversation, GameAdapter | None, Director | None]:
        name = args.game or self.config.game
        adapter = None
        director = None
        if name:
            adapter = load("games", name, self.config.games.get(name, {}))
            self.stack.push_async_callback(close_backend, adapter)
            director = Director(self.persona.chattiness, self.config.director)
        conversation = Conversation(
            self.backend("llm", self.config.llm),
            self.persona,
            self.language,
            TranscriptLog(self.config.transcript_dir),
            context=adapter.describe if adapter is not None else None,
            game=adapter.game if adapter is not None else None,
        )
        return conversation, adapter, director

    def speech(self, kind: str) -> Any:
        spec = getattr(self.config, kind)
        if spec is None:
            raise ConfigError(f"Configure [{kind}] backend before using speech commands")
        return self.backend(kind, spec)


@asynccontextmanager
async def _runtime(args: argparse.Namespace) -> AsyncIterator[_Runtime]:
    config = load_config(args.config)
    persona = load_persona(
        config.personas_dir, args.persona or config.persona, config.user_personas_dir
    )
    language = args.language or config.language
    persona.system_prompt(language)
    async with AsyncExitStack() as stack:
        yield _Runtime(config, persona, language, stack)


async def _chat(args: argparse.Namespace) -> None:
    async with _runtime(args) as runtime:
        conversation, adapter, director = runtime.conversation(args)
        interjection: asyncio.Task | None = None
        reply_lock = asyncio.Lock()
        user_busy = False
        tasks: list[asyncio.Task] = []

        async def cancel_interjection() -> None:
            if interjection is not None:
                interjection.cancel()
                await asyncio.gather(interjection, return_exceptions=True)
                if director is not None:
                    director.set_busy("speaking", False)
                    director.set_busy("critical", False)

        async def speak_event(event: GameEvent) -> None:
            assert director is not None
            critical = director.preempts(event)
            director.set_busy("critical", critical)
            try:
                await _reply(conversation, director=director, event=event)
            except _ERRORS as exc:
                print(f"Error: {exc}", file=sys.stderr)
            finally:
                director.set_busy("critical", False)

        async def events() -> None:
            nonlocal interjection
            assert director is not None
            while True:
                event = await director.next()
                async with reply_lock:
                    await cancel_interjection()
                    if user_busy:
                        director.submit(event)
                        continue
                    director.set_busy("speaking", True)
                    director.set_busy("critical", director.preempts(event))
                    interjection = asyncio.create_task(speak_event(event))

        if adapter is not None and director is not None:
            tasks = [
                asyncio.create_task(feed_events(adapter, director)),
                asyncio.create_task(events()),
            ]
        print(f"Chatting with {runtime.persona.display_name}. Type /quit to exit.")
        try:
            while True:
                try:
                    text = (await asyncio.to_thread(input, "> ")).strip()
                except EOFError:
                    print()
                    return
                if text == "/quit":
                    return
                if not text:
                    continue
                user_busy = True
                if director is not None:
                    director.set_busy("user", True)
                async with reply_lock:
                    await cancel_interjection()
                try:
                    await _cancellable_reply(conversation, text, director)
                finally:
                    user_busy = False
                    if director is not None:
                        director.set_busy("user", False)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await cancel_interjection()


def _elevenlabs(backend: Any) -> Any:
    if backend.name == "elevenlabs":
        return backend
    if isinstance(backend, FallbackTTS):
        return _elevenlabs(backend.primary) or _elevenlabs(backend.secondary)
    return None


async def _print_quota(backend: Any) -> None:
    used, limit = await backend.quota()
    remaining = max(0, limit - used)
    print(f"ElevenLabs: {remaining:,} of {limit:,} characters remaining.")
    if limit and remaining / limit < 0.1:
        print("Warning: ElevenLabs quota below 10%.")


async def _voice(args: argparse.Namespace) -> None:
    async with _runtime(args) as runtime:
        stt = runtime.speech("stt")
        tts = runtime.speech("tts")
        elevenlabs = _elevenlabs(tts)
        if elevenlabs is not None:
            try:
                await _print_quota(elevenlabs)
            except BackendError as exc:
                print(f"Cannot check ElevenLabs quota: {exc}", file=sys.stderr)
        conversation, adapter, director = runtime.conversation(args)
        transcript = conversation.transcript
        ptt = runtime.backend("ptt", runtime.config.ptt)
        # VoiceSession owns cleanup once started; remove the setup cleanup callbacks.
        runtime.stack.pop_all()
        await VoiceSession(
            ptt,
            Recorder(),
            Player(),
            stt,
            tts,
            conversation,
            runtime.persona,
            runtime.language,
            transcript,
            adapter=adapter,
            director=director,
        ).run()


async def _say(args: argparse.Namespace) -> None:
    async with _runtime(args) as runtime:
        tts = runtime.speech("tts")
        player = Player()
        runtime.stack.push_async_callback(player.aclose)
        voice = runtime.persona.voice(tts.name, runtime.language)
        await player.play(tts.synthesize(args.text, voice, runtime.language))
        await player.wait_idle()


async def _voices(args: argparse.Namespace) -> None:
    from familiar.backends.elevenlabs.tts import ElevenLabsTTS

    config = load_config(args.config)
    spec = config.tts
    while spec is not None and spec.backend != "elevenlabs":
        spec = spec.fallback
    backend = ElevenLabsTTS(spec.settings if spec is not None else {})
    try:
        for voice in await backend.voices():
            print(f"{voice['voice_id']}  {voice['name']}  {voice.get('labels', {})}")
        await _print_quota(backend)
    finally:
        await backend.aclose()


async def _ptt_setup(args: argparse.Namespace) -> None:
    from familiar.config import USER_CONFIG, find_config_path
    from familiar.config_writer import save_ptt
    from familiar.ptt.setup import detect_button

    print("Press the joystick button you want for push-to-talk (30 s)…")
    print("(Buttons already held down, like a lever resting in a detent, are ignored.)")
    device, button = await detect_button()
    print(f"Got {button} on {device}. Press it once more to confirm…")
    confirmation = await detect_button()
    if confirmation != (device, button):
        print(f"That was {confirmation[1]} on {confirmation[0]}; nothing saved. Run it again.")
        raise SystemExit(1)
    path = find_config_path(args.config) or USER_CONFIG.expanduser()
    save_ptt(path, device, button)
    print(f"Saved to {path}: push-to-talk = {button} on {device}.")


async def _gui(args: argparse.Namespace) -> None:
    from familiar.gui.server import serve

    await serve(args.port, args.no_browser, args.config)


async def _watch(args: argparse.Namespace) -> None:
    from familiar.games.scs.tools import watch

    config = load_config(args.config)
    game = args.game or config.game or "ats"
    settings = dict(config.games.get(game, {}))
    if args.replay is not None:
        settings["replay"] = args.replay
    await watch(settings, game)


async def _telemetry_record(args: argparse.Namespace) -> None:
    from familiar.games.scs.tools import record

    config = load_config(args.config)
    game = args.game or config.game or "ats"
    await record(config.games.get(game, {}), args.file)


def _landmark_arguments(commands: Any) -> None:
    from pathlib import Path

    landmarks = commands.add_parser("landmarks", help="Curate landmark knowledge packs")
    actions = landmarks.add_subparsers(dest="landmark_action", required=True)
    status = actions.add_parser("status", help="Show installed and covered ATS states")
    status.add_argument("--game", choices=["ats"], default="ats")
    status.add_argument("--game-dir", type=Path)
    add = actions.add_parser("add", help="Research, check and merge new states")
    add.add_argument("states", nargs="+", type=str.upper, metavar="XX")
    add.add_argument("--per-state", type=int, default=6)
    add.add_argument("--pack", type=Path)
    add.add_argument("--dry-run", action="store_true")
    add.add_argument("--workdir", type=Path)
    check = actions.add_parser("check", help="Check a pack without modifying it")
    check.add_argument("pack", nargs="?", type=Path)
    for command in (status, add, check):
        command.add_argument("--config")


def _landmark_status(args: argparse.Namespace) -> None:
    from pathlib import Path

    from familiar.games.scs.curation import owned_states, pack_states
    from familiar.games.scs.knowledge import KnowledgePack
    from familiar.games.scs.places import STATE_NAMES

    config = load_config(args.config)
    directory = args.game_dir or Path(
        config.games.get(args.game, {}).get(
            "game_dir", "~/.local/share/Steam/steamapps/common/American Truck Simulator"
        )
    )
    owned = owned_states(directory.expanduser())
    covered = pack_states(KnowledgePack.for_game(args.game).landmarks)
    missing = owned - covered
    for label, states in (("Owned", owned), ("Covered", covered), ("Missing", missing)):
        names = ", ".join(f"{code} ({STATE_NAMES.get(code, code)})" for code in sorted(states))
        print(f"{label}: {names or '-'}")
    if missing:
        print(f"Next: familiar landmarks add {' '.join(sorted(missing))}")


def _landmark_summary(original: list[dict], kept: list[dict]) -> None:
    from collections import Counter

    from familiar.games.scs.curation import pack_states

    originals = {entry["id"]: entry for entry in original}
    totals = Counter(entry["state"] for entry in original)
    counts: Counter[str] = Counter()
    corrected: Counter[str] = Counter()
    removed: Counter[str] = Counter()
    for entry in kept:
        state = entry["state"]
        before = set(originals[entry["id"]]["facts"])
        after = set(entry["facts"])
        counts[state] += 1
        corrected[state] += len(after - before)
        removed[state] += len(before - after)
    for state in sorted(pack_states(original)):
        print(
            f"{state}: kept {counts[state]}, dropped {totals[state] - counts[state]}, "
            f"facts corrected {corrected[state]}, facts removed {removed[state]}"
        )


async def _landmarks(args: argparse.Namespace) -> None:
    import httpx

    if args.landmark_action == "status":
        _landmark_status(args)
        return
    try:
        await _curate_landmarks(args)
    except (OSError, ValueError, httpx.HTTPError, TimeoutError) as exc:
        raise ConfigError(f"Landmark curation failed: {exc}") from exc


async def _curate_landmarks(args: argparse.Namespace) -> None:
    from datetime import UTC, datetime
    from pathlib import Path

    import httpx

    from familiar.games.scs import curation

    adding = args.landmark_action == "add"
    pack = args.pack or curation.DEFAULT_PACK
    workdir = Path("data/landmarks") / datetime.now(UTC).isoformat().replace("+00:00", "Z")
    if adding:
        workdir = args.workdir or workdir
        entries = await curation.research(args.states, args.per_state, workdir)
    if not adding:
        entries = curation.read_pack(pack)
    workdir.mkdir(parents=True, exist_ok=True)
    report_path = workdir / "factcheck.json"
    with httpx.Client(headers=curation.HEADERS, timeout=30) as client:
        statuses = curation.verify_coordinates(entries, client)
        rejected = [entry for entry in entries if statuses[entry["id"]] != "OK"]
        for entry in rejected:
            print(f"{entry['id']}: {statuses[entry['id']]}")
        print(f"Coordinates: {len(entries) - len(rejected)}/{len(entries)} OK")
        candidates = [entry for entry in entries if statuses[entry["id"]] == "OK"]
        if not adding:
            candidates = entries
        config = load_config(args.config)
        llm = load("llm", config.llm.backend, {**config.llm.settings, "temperature": 0})
        try:
            report = await curation.factcheck(candidates, llm, client, 4.0, report_path)
        finally:
            await close_backend(llm)
    kept, left_out = curation.apply_report(candidates, report)
    _landmark_summary(entries, kept)
    print(f"Fact-check left out: {', '.join(left_out) or '-'}")
    print(f"Report: {report_path}")
    if not adding:
        return
    staged = workdir / "landmarks.checked.yaml"
    curation.write_pack(staged, kept)
    print(f"Staged: {staged}")
    if args.dry_run:
        return
    added, skipped = curation.merge(pack, kept)
    print(f"Merged into {pack}: added {added}, skipped {skipped}")


def main() -> None:
    """Dispatch the text REPL or list installed backend entry points."""
    parser = argparse.ArgumentParser(prog="familiar")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in {
        "chat": "Chat with your companion",
        "voice": "Push-to-talk voice conversation",
        "say": "Speak text with the configured voice",
    }.items():
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--config")
        command.add_argument("--persona")
        command.add_argument("--language")
        if name == "say":
            command.add_argument("text")
        if name in {"chat", "voice"}:
            command.add_argument("--game", metavar="NAME")
    commands.add_parser("plugins", help="List installed backends")
    gui = commands.add_parser("gui", help="Open the Familiar launcher")
    gui.add_argument("--port", type=int, default=8765)
    gui.add_argument("--no-browser", action="store_true")
    gui.add_argument("--config")
    ptt_setup = commands.add_parser("ptt-setup", help="Detect and save a joystick PTT button")
    ptt_setup.add_argument("--config")
    voices = commands.add_parser("voices", help="List ElevenLabs voices and quota")
    voices.add_argument("--config")
    watch = commands.add_parser("watch", help="Watch live game telemetry")
    watch.add_argument("--config")
    watch.add_argument("--game", required=True, metavar="NAME")
    watch.add_argument("--replay", metavar="FILE")
    record = commands.add_parser("telemetry-record", help="Record game telemetry")
    record.add_argument("file", metavar="FILE")
    record.add_argument("--config")
    record.add_argument("--game", metavar="NAME")
    _landmark_arguments(commands)
    args = parser.parse_args()
    try:
        if args.command == "plugins":
            for kind in GROUPS:
                print(f"{kind}: {', '.join(available(kind)) or '(none)'}")
            return
        handlers = {
            "landmarks": _landmarks,
            "gui": _gui,
            "watch": _watch,
            "telemetry-record": _telemetry_record,
            "chat": _chat,
            "voice": _voice,
            "say": _say,
            "voices": _voices,
            "ptt-setup": _ptt_setup,
        }
        asyncio.run(handlers[args.command](args))
    except _ERRORS as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print()
