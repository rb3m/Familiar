"""Command-line entry points for backend discovery and interactive chat."""

import argparse
import asyncio
import signal
import sys

from familiar.config import load_config
from familiar.conversation import Conversation
from familiar.interfaces import BackendError, ConfigError, PluginError
from familiar.persona import load_persona
from familiar.plugins import GROUPS, available, load
from familiar.transcript import TranscriptLog

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


async def _chat(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    persona = load_persona(config.personas_dir, args.persona or config.persona)
    language = args.language or config.language
    persona.system_prompt(language)
    llm = load("llm", config.llm.backend, config.llm.settings)
    conversation = Conversation(llm, persona, language, TranscriptLog(config.transcript_dir))
    print(f"Chatting with {persona.display_name}. Type /quit to exit.")
    try:
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
    finally:
        close = getattr(llm, "aclose", None)
        if close is not None:
            await close()


def main() -> None:
    """Dispatch the text REPL or list installed backend entry points."""
    parser = argparse.ArgumentParser(prog="familiar")
    commands = parser.add_subparsers(dest="command", required=True)
    chat = commands.add_parser("chat", help="Chat with your companion")
    chat.add_argument("--config")
    chat.add_argument("--persona")
    chat.add_argument("--language")
    commands.add_parser("plugins", help="List installed backends")
    args = parser.parse_args()
    try:
        if args.command == "plugins":
            for kind in GROUPS:
                print(f"{kind}: {', '.join(available(kind)) or '(none)'}")
            return
        asyncio.run(_chat(args))
    except _ERRORS as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print()
