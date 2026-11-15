"""Coordinate streamed replies, bounded history, and transcript events."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import asdict

from familiar.interfaces import GameEvent, LLMProvider, Message, TextToSpeech
from familiar.persona import Persona
from familiar.pipeline.sentences import SentenceSplitter
from familiar.transcript import TranscriptLog


class Conversation:
    def __init__(
        self,
        llm: LLMProvider,
        persona: Persona,
        language: str,
        transcript: TranscriptLog,
        tts: TextToSpeech | None = None,
        history_limit: int = 40,
        context: Callable[[], str] | None = None,
        game: str | None = None,
    ) -> None:
        if history_limit < 1:
            raise ValueError("history_limit must be positive")
        self.llm = llm
        self.persona = persona
        self.language = language
        self.transcript = transcript
        self.tts = tts
        self.context = context
        self.game = game
        self.history_limit = history_limit
        self.history: list[Message] = []

    def _remember(self, message: Message) -> None:
        self.history.append(message)
        del self.history[: -self.history_limit]

    async def ask(self, user_text: str) -> AsyncIterator[str]:
        """Yield reply sentences and retain partial replies on cancellation."""
        self.transcript.record("user", text=user_text)
        async with aclosing(self._turn(user_text)) as sentences:
            async for sentence in sentences:
                yield sentence

    async def interject(self, event: GameEvent) -> AsyncIterator[str]:
        """React to a grounded game event using the ordinary reply pipeline."""
        summary = event.data["summary"]
        self.transcript.record(
            "event", event_kind=event.kind, priority=event.priority, summary=summary
        )
        text = (
            "[Game event, not said by the player. React briefly and in character; "
            "one or two sentences.] " + summary
        )
        facts = event.data.get("facts")
        if facts:
            text += "\nFacts you may use: - " + "\n- ".join(facts)
        async with aclosing(self._turn(text)) as sentences:
            async for sentence in sentences:
                yield sentence

    async def _turn(self, text: str) -> AsyncIterator[str]:
        system = self.persona.system_prompt(self.language, self.game)
        context = self.context() if self.context is not None else ""
        if context:
            system += "\n\nCurrent situation (exact, from live telemetry):\n" + context
        self._remember(Message("user", text))
        splitter = SentenceSplitter()
        fragments: list[str] = []
        interrupted = False
        try:
            async for chunk in self.llm.stream(list(self.history), system, []):
                if chunk.tool_call is not None:
                    self.transcript.record("tool_call", **asdict(chunk.tool_call))
                fragments.append(chunk.text)
                for sentence in splitter.feed(chunk.text):
                    yield sentence
            remainder = splitter.flush()
            if remainder:
                yield remainder
        except (asyncio.CancelledError, GeneratorExit):
            interrupted = True
            raise
        finally:
            text = "".join(fragments)
            fields = {"interrupted": True} if interrupted else {}
            self.transcript.record("assistant", text=text, **fields)
            if text:
                self._remember(Message("assistant", text))
