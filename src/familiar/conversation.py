"""Coordinate streamed replies, bounded history, and transcript events."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import asdict

from familiar.interfaces import LLMProvider, Message, TextToSpeech
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
    ) -> None:
        if history_limit < 1:
            raise ValueError("history_limit must be positive")
        self.llm = llm
        self.persona = persona
        self.language = language
        self.transcript = transcript
        self.tts = tts
        self.history_limit = history_limit
        self.history: list[Message] = []

    def _remember(self, message: Message) -> None:
        self.history.append(message)
        del self.history[: -self.history_limit]

    async def ask(self, user_text: str) -> AsyncIterator[str]:
        """Yield reply sentences and retain partial replies on cancellation."""
        system = self.persona.system_prompt(self.language)
        self.transcript.record("user", text=user_text)
        self._remember(Message("user", user_text))
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
