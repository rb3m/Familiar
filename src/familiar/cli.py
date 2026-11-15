"""Command-line entry points for backend discovery and interactive chat."""

import argparse
import asyncio
import signal
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any

from familiar.audio.player import Player
from familiar.audio.recorder import Recorder
from familiar.config import AppConfig, BackendConfig, load_config
from familiar.conversation import Conversation
from familiar.fallback import FallbackSTT, FallbackTTS, close_backend
from familiar.interfaces import BackendError, ConfigError, PluginError
from familiar.persona import Persona, load_persona
from familiar.plugins import GROUPS, available, load
from familiar.transcript import TranscriptLog
from familiar.voice import VoiceSession

_ERRORS = (ConfigError, PluginError, BackendError)


async def _reply(conversation: Conversation, text: str) -> None:
    print(f"{conversation.persona.display_name}: ", end="", flush=True)
    async for sentence in conversation.ask(text):
        print(sentence, end=" ", flush=True)
    print()


async def _cancellable_reply(conversation: Conversation, text: str) -> None:
    task = asyncio.create_task(_reply(conversation, text))
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

    def speech(self, kind: str) -> Any:
        spec = getattr(self.config, kind)
        if spec is None:
            raise ConfigError(f"Configure [{kind}] backend before using speech commands")
        return self.backend(kind, spec)


@asynccontextmanager
async def _runtime(args: argparse.Namespace) -> AsyncIterator[_Runtime]:
    config = load_config(args.config)
    persona = load_persona(config.personas_dir, args.persona or config.persona)
    language = args.language or config.language
    persona.system_prompt(language)
    async with AsyncExitStack() as stack:
        yield _Runtime(config, persona, language, stack)


async def _chat(args: argparse.Namespace) -> None:
    async with _runtime(args) as runtime:
        llm = runtime.backend("llm", runtime.config.llm)
        conversation = Conversation(
            llm,
            runtime.persona,
            runtime.language,
            TranscriptLog(runtime.config.transcript_dir),
        )
        print(f"Chatting with {runtime.persona.display_name}. Type /quit to exit.")
        while True:
            try:
                text = input("> ").strip()
            except EOFError:
                print()
                return
            if text == "/quit":
                return
            if text:
                await _cancellable_reply(conversation, text)


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
        llm = runtime.backend("llm", runtime.config.llm)
        transcript = TranscriptLog(runtime.config.transcript_dir)
        conversation = Conversation(llm, runtime.persona, runtime.language, transcript)
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
    commands.add_parser("plugins", help="List installed backends")
    ptt_setup = commands.add_parser("ptt-setup", help="Detect and save a joystick PTT button")
    ptt_setup.add_argument("--config")
    voices = commands.add_parser("voices", help="List ElevenLabs voices and quota")
    voices.add_argument("--config")
    args = parser.parse_args()
    try:
        if args.command == "plugins":
            for kind in GROUPS:
                print(f"{kind}: {', '.join(available(kind)) or '(none)'}")
            return
        handlers = {
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
