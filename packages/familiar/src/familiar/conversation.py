"""Coordinate streamed replies, bounded history, and transcript events."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import asdict

from familiar.flavor import FlavorKeeper, flavor_hint
from familiar.interfaces import GameEvent, LLMProvider, Message, TextToSpeech
from familiar.memory import MemoryKeeper
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
        memory: MemoryKeeper | None = None,
        flavor: FlavorKeeper | None = None,
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
        self.memory = memory
        self.flavor = flavor
        self.history_limit = history_limit
        self.history: list[Message] = []

    def _remember(self, message: Message) -> None:
        self.history.append(message)
        del self.history[: -self.history_limit]

    async def ask(self, user_text: str) -> AsyncIterator[str]:
        """Yield reply sentences and retain partial replies on cancellation."""
        self.transcript.record("user", text=user_text)
        async with aclosing(self._turn(user_text, player_turn=True)) as sentences:
            async for sentence in sentences:
                yield sentence

    async def interject(
        self, event: GameEvent, spoken: Callable[[], bool] | None = None
    ) -> AsyncIterator[str]:
        """React to a grounded game event using the ordinary reply pipeline."""
        if (
            self.memory is not None
            and "memory_episode_id" in event.data
            and not self.memory.recall_ready(event.data["memory_episode_id"])
        ):
            return
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
        candidate = self.flavor.choose(event) if self.flavor is not None else None
        if candidate is not None:
            text += flavor_hint(candidate)
        recorded = False
        async with aclosing(self._turn(text)) as sentences:
            async for sentence in sentences:
                yield sentence
                if recorded or (spoken is not None and not spoken()):
                    continue
                self._record_interjection(event, candidate)
                recorded = True

    def _record_interjection(self, event: GameEvent, candidate: dict | None) -> None:
        if self.memory is not None and "memory_episode_id" in event.data:
            self.memory.recalled(event)
        if self.flavor is not None and candidate is not None:
            self.flavor.used(self.game or "", candidate)

    async def aclose(self) -> None:
        if self.memory is not None:
            await self.memory.finish(self.llm, self.transcript)

    async def _turn(self, text: str, player_turn: bool = False) -> AsyncIterator[str]:
        system = self.persona.system_prompt(self.language, self.game)
        context = self.context() if self.context is not None else ""
        if context:
            system += "\n\nCurrent situation (exact, from live telemetry):\n" + context
        memories = ""
        if player_turn and self.memory is not None:
            memories = self.memory.context(text)
        if memories:
            system += "\n\n" + memories
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
