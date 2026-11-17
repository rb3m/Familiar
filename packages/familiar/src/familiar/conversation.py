"""Coordinate streamed replies, bounded history, and transcript events."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import asdict

from familiar.alerts import AlertKeeper
from familiar.flavor import FlavorKeeper, flavor_hint
from familiar.interfaces import GameEvent, LLMProvider, Message, TextToSpeech
from familiar.memory import MemoryKeeper
from familiar.persona import Persona
from familiar.pipeline.sentences import SentenceSplitter
from familiar.tools import ToolRegistry
from familiar.transcript import TranscriptLog

ALERT_RULE = (
    "\n\nAlerts: you cannot watch telemetry on your own. Whenever the player asks to be "
    "told, warned or called out when a value is reached, call set_alert in this same reply "
    "before saying anything about it, and confirm only what its result says. Never claim an "
    "alert is set unless set_alert returned it."
)


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
        tools: ToolRegistry | None = None,
        alerts: AlertKeeper | None = None,
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
        self.tools = tools if tools is not None else ToolRegistry()
        self.alerts = alerts
        self.history_limit = history_limit
        self.history: list[Message] = []

    def _remember(self, message: Message) -> None:
        self._remember_many([message])

    def _remember_many(self, messages: list[Message]) -> None:
        self.history.extend(messages)
        start = max(0, len(self.history) - self.history_limit)
        # The history limit is soft for an indivisible call/results group.
        while start > 0 and self.history[start].role == "tool":
            start -= 1
        del self.history[:start]

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
        speak = event.data.get("speak")
        if speak:
            # Pre-written line (e.g. an alert): no LLM round trip, so it lands on time.
            self.transcript.record("assistant", text=speak)
            self._remember_many(
                [Message("user", "[Game event] " + summary), Message("assistant", speak)]
            )
            yield speak
            return
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
        specs = self.tools.specs() if player_turn else []
        if any(spec.name == "set_alert" for spec in specs):
            system += ALERT_RULE
        self._remember(Message("user", text))
        for round_index in range(4):
            calls = []
            offered = specs if round_index < 3 else []
            async with aclosing(self._stream_round(system, offered, calls)) as sentences:
                async for sentence in sentences:
                    yield sentence
            if not calls:
                return
            results = []
            try:
                for call in calls:
                    result = {"error": "Tools are unavailable for this response"}
                    if offered:
                        result = await self.tools.execute(call)
                    payload = json.dumps(result, separators=(",", ":"), ensure_ascii=False)
                    self.transcript.record("tool_result", tool_call_id=call.id, text=payload)
                    results.append(Message("tool", payload, tool_call_id=call.id))
            finally:
                # Cancellation still leaves a complete protocol group for the next turn.
                for call in calls[len(results) :]:
                    payload = '{"error":"Tool execution interrupted"}'
                    self.transcript.record("tool_result", tool_call_id=call.id, text=payload)
                    results.append(Message("tool", payload, tool_call_id=call.id))
                self._remember_many(results)
            if not offered:
                return

    async def _stream_round(self, system: str, tools: list, calls: list) -> AsyncIterator[str]:
        splitter = SentenceSplitter()
        fragments: list[str] = []
        interrupted = False
        completed = False
        try:
            async for chunk in self.llm.stream(list(self.history), system, tools):
                if chunk.tool_call is not None:
                    self.transcript.record("tool_call", **asdict(chunk.tool_call))
                    calls.append(chunk.tool_call)
                fragments.append(chunk.text)
                for sentence in splitter.feed(chunk.text):
                    yield sentence
            remainder = splitter.flush()
            if remainder:
                yield remainder
            completed = True
        except (asyncio.CancelledError, GeneratorExit):
            interrupted = True
            raise
        finally:
            text = "".join(fragments)
            fields = {"interrupted": True} if interrupted else {}
            self.transcript.record("assistant", text=text, **fields)
            if text or calls:
                # Defer trimming until results have been attached to their calls.
                self.history.append(Message("assistant", text, tool_calls=tuple(calls)))
            if not calls:
                self._remember_many([])
            if not completed and calls:
                results = [
                    Message("tool", '{"error":"Response interrupted"}', tool_call_id=call.id)
                    for call in calls
                ]
                for result in results:
                    self.transcript.record(
                        "tool_result", tool_call_id=result.tool_call_id, text=result.text
                    )
                self._remember_many(results)
