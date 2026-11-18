"""Coordinate streamed replies, bounded history, and transcript events."""

import json
import logging
import random
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import asdict

from familiar.alerts import AlertKeeper
from familiar.banter import Casting, parse_scene
from familiar.crew import Crew, persona_id
from familiar.fallback import llm_budget_blocked
from familiar.flavor import FlavorKeeper, flavor_hint
from familiar.hands import ActionKeeper
from familiar.history import DefaultHistoryRenderer, HistoryRecord, HistoryRenderer
from familiar.interfaces import (
    GameEvent,
    LLMProvider,
    Message,
    TextToSpeech,
    ToolCall,
    ToolSpec,
)
from familiar.memory import MemoryKeeper
from familiar.memory.keeper import CREW_REPLY_WINDOW
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


ACTION_RULE = (
    "\n\nActions: act only when the player asks in this turn. Call the act_ tool right "
    "away, even for risky actions; never ask for confirmation before calling it. When an "
    "action returns needs_confirmation, ask its ask line as a short yes/no question and wait. Call "
    "confirm_action only after the player answers yes in a later message. Never say an "
    "action happened unless its status is done or already. Say plainly when it is "
    "unavailable or unknown."
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
        actions: ActionKeeper | None = None,
        crew: Crew | None = None,
        renderer: HistoryRenderer | None = None,
    ) -> None:
        if history_limit < 1:
            raise ValueError("history_limit must be positive")
        self.llm = llm
        self.persona = persona
        self.crew = crew or Crew([persona], persona_id(persona))
        self.speaker = self.crew.carrier
        self.renderer = renderer or DefaultHistoryRenderer()
        self.rng = random.Random()
        self.language = language
        self.transcript = transcript
        self.tts = tts
        self.context = context
        self.game = game
        self.memory = memory
        self.flavor = flavor
        self.tools = tools if tools is not None else ToolRegistry()
        self.alerts = alerts
        self.actions = actions
        self.history_limit = history_limit
        self.records: list[HistoryRecord] = []
        self.playback_ack_required = False
        self._playback_acks = 0
        self.casting = Casting()
        self._crew_reply_candidate = None
        self.banter_started: Callable[[], None] = lambda: None

    def acknowledge_playback(self) -> None:
        """The voice consumer calls this only after a sentence's audio drains."""
        self._playback_acks += 1

    def _was_heard(self, before: int) -> bool:
        return not self.playback_ack_required or self._playback_acks > before

    @property
    def history(self) -> list[Message]:
        """Unrendered protocol view retained for callers inspecting history."""
        return [record.message() for record in self.records]

    def _record_message(
        self, message: Message, *, heard: bool = True, kind: str | None = None
    ) -> None:
        speaker = persona_id(self.speaker)
        if message.role == "user":
            speaker = "player"
        kinds = {"user": "player", "assistant": "assistant", "tool": "tool_result"}
        self.records.append(
            HistoryRecord(
                speaker,
                kind or kinds[message.role],
                message.text,
                heard,
                message.tool_calls,
                message.tool_call_id,
            )
        )

    def _remember(self, message: Message) -> None:
        self._remember_many([message])

    def _remember_many(self, messages: list[Message]) -> None:
        for message in messages:
            self._record_message(message)
        start = max(0, len(self.records) - self.history_limit)
        # The history limit is soft for an indivisible call/results group.
        while start > 0 and self.records[start].kind == "tool_result":
            start -= 1
        del self.records[:start]

    async def ask(self, user_text: str) -> AsyncIterator[str]:
        """Yield reply sentences and retain partial replies on cancellation."""
        if self.actions is not None:
            self.actions.begin_turn(user_text)
        self.speaker = self.crew.pick_for_utterance(user_text, self.language)
        self._remember_crew_reply()
        self.transcript.record(
            "user", text=user_text, speaker_id="player", source="player_observed"
        )
        async with aclosing(self._turn(user_text, player_turn=True)) as sentences:
            async for sentence in sentences:
                yield sentence

    async def interject(
        self, event: GameEvent, spoken: Callable[[], bool] | None = None
    ) -> AsyncIterator[str]:
        """React to a grounded game event using the ordinary reply pipeline."""
        if event.kind == "banter":
            async with aclosing(self.banter_scene()) as lines:
                async for line in lines:
                    yield line
            return
        if (
            self.memory is not None
            and "memory_episode_id" in event.data
            and not self.memory.recall_ready(event.data["memory_episode_id"])
        ):
            return
        summary = event.data["summary"]
        self.speaker = self._event_speaker(event)
        self.transcript.record(
            "event", event_kind=event.kind, priority=event.priority, summary=summary
        )
        speak = event.data.get("speak")
        if speak:
            # Pre-written line (e.g. an alert): no LLM round trip, so it lands on time.
            speak = self.crew.strip_prefix(speak, self.language)
            self._record_message(Message("user", "[Game event] " + summary), kind="game_event")
            heard = False
            before = self._playback_acks
            try:
                yield speak
                heard = self._was_heard(before) and (spoken is None or spoken())
                self._note_heard(heard)
            finally:
                self._record_message(Message("assistant", speak), heard=heard)
                self._remember_many([])
                self._log_line(speak, heard)
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

    def banter_blocked(self) -> str | None:
        if self.actions is not None and self.actions.pending is not None:
            return "pending action confirmation"
        return llm_budget_blocked(self.llm)

    async def banter_scene(self) -> AsyncIterator[str]:
        reason = self.banter_blocked()
        if reason:
            logging.getLogger(__name__).debug("Skipping banter: %s", reason)
            return
        pair = self.casting.pick(self.crew, self.rng)
        renderer = DefaultHistoryRenderer()
        situation = "desk mode"
        if self.context is not None:
            situation = self.context()
        prompt = (
            f"Write a short crew banter scene in session language {self.language}.\n"
            f"Initiator: {pair[0]}; partner: {pair[1]}. Both must speak. "
            "A third crew member may join for at most one line.\n"
            "Output ONLY 2-4 short spoken lines, aiming for 2-3, each as "
            "<persona_id>: <text>, with actual IDs and no angle brackets. "
            "No stage directions, player lines or claims to take game actions. "
            "Strict members use only supplied facts. Playful members may joke but must "
            "never misreport game state. Practical information must always be exact.\n"
            "Crew roster (third-person roles and relationships):\n"
            + renderer.roster(self.crew, self.game)
            + "\nCurrent situation: "
            + situation
            + "\nRecent chronological transcript (unheard lines were NOT said):\n"
            + renderer.chronological(self.records, self.crew)
        )
        if self.memory is not None:
            prompt += self.memory.crew_callback()
        async with aclosing(
            self.llm.stream([Message("user", "Write the scene.")], prompt, [])
        ) as source:
            chunks = [chunk async for chunk in source]
        if any(chunk.tool_call is not None for chunk in chunks):
            logging.getLogger(__name__).debug("Dropping banter: unexpected tool call")
            return
        lines = parse_scene("".join(chunk.text for chunk in chunks), self.crew, self.language)
        speakers = {speaker for speaker, _ in lines}
        extras = [speaker for speaker, _ in lines if speaker not in pair]
        if not lines or not set(pair).issubset(speakers) or len(extras) > 1:
            logging.getLogger(__name__).debug("Dropping malformed banter scene")
            return
        self.banter_started()
        self._crew_reply_candidate = None
        records = [HistoryRecord(key, "banter", text, False) for key, text in lines]
        members = {persona_id(member): member for member in self.crew.members}
        try:
            for index, record in enumerate(records):
                self.speaker = members[record.speaker_id]
                before = self._playback_acks
                resumed = False
                try:
                    yield record.text
                    resumed = True
                finally:
                    heard = self._was_heard(before) and (resumed or self.playback_ack_required)
                    records[index] = HistoryRecord(record.speaker_id, "banter", record.text, heard)
                    self._note_heard(heard)
                    self._offer_crew_reply(record.speaker_id, record.text, heard)
        finally:
            self._remember_scene(records)
            self.records.extend(records)
            self._remember_many([])
            for record in records:
                self.transcript.record(
                    "banter",
                    text=record.text,
                    speaker_id=record.speaker_id,
                    heard=record.heard,
                    source="persona_claim",
                )

    def _remember_scene(self, records: list[HistoryRecord]) -> None:
        if self.memory is None or not all(record.heard for record in records):
            return
        self._crew_reply_candidate = None
        self.memory.remember_crew([(record.speaker_id, record.text) for record in records])

    def _offer_crew_reply(self, speaker: str, text: str, heard: bool) -> None:
        if self.memory is None or not heard or not text:
            return
        self._crew_reply_candidate = (speaker, text, self.memory.clock())

    def _remember_crew_reply(self) -> None:
        candidate = self._crew_reply_candidate
        self._crew_reply_candidate = None
        if self.memory is None or candidate is None:
            return
        speaker, text, timestamp = candidate
        if speaker != persona_id(self.speaker):
            return
        if self.memory.clock() - timestamp > CREW_REPLY_WINDOW:
            return
        self.memory.remember_crew([(speaker, text)])

    def _event_speaker(self, event: GameEvent) -> Persona:
        if event.priority == "critical":
            return self.crew.carrier
        assigned = event.data.get("speaker_id")
        member = next(
            (member for member in self.crew.members if persona_id(member) == assigned), None
        )
        if member is not None:
            return member
        if event.kind == "idle":
            return self.crew.pick_idle(self.rng)
        return self.crew.pick_for_event(event)

    def _log_line(self, text: str, heard: bool) -> None:
        self._crew_reply_candidate = None
        self._offer_crew_reply(persona_id(self.speaker), text, heard)
        fields = {}
        if not heard:
            fields["interrupted"] = True
        self.transcript.record(
            "assistant",
            text=text,
            speaker_id=persona_id(self.speaker),
            heard=heard,
            source="persona_claim",
            **fields,
        )

    def _record_interjection(self, event: GameEvent, candidate: dict | None) -> None:
        if self.memory is not None and "memory_episode_id" in event.data:
            self._recall_memory(event)
        if self.flavor is not None and candidate is not None:
            self.flavor.used(self.game or "", candidate)

    def _recall_memory(self, event: GameEvent) -> None:
        if self.memory is None:
            return
        if len(self.crew.members) == 1:
            self.memory.recalled(event)
            return
        self.memory.recalled(event, speaker=persona_id(self.speaker))

    async def aclose(self) -> None:
        if self.memory is not None:
            await self.memory.finish(self.llm, self.transcript)

    async def _turn(self, text: str, player_turn: bool = False) -> AsyncIterator[str]:
        kinds = {True: "player", False: "game_event"}
        self._record_message(Message("user", text), kind=kinds[player_turn])
        self._remember_many([])
        system = ""
        context = self.context() if self.context is not None else ""
        if context:
            system += "\n\nCurrent situation (exact, from live telemetry):\n" + context
        memories = ""
        if player_turn and self.memory is not None:
            memories = self.memory.context(text)
        if memories:
            system += "\n\n" + memories
        specs = []
        if player_turn:
            specs = self.tools.specs(carrier=self.speaker == self.crew.carrier)
        if any(spec.name == "set_alert" for spec in specs):
            system += ALERT_RULE
        if any(spec.name.startswith("act_") for spec in specs):
            system += ACTION_RULE
        async with aclosing(self._reply_round(system, specs)) as sentences:
            async for sentence in sentences:
                yield sentence

    async def _reply_round(
        self, system: str, specs: list[ToolSpec], round_index: int = 0
    ) -> AsyncIterator[str]:
        calls: list[ToolCall] = []
        offered = []
        if round_index < 3:
            offered = specs
        async with aclosing(self._stream_round(system, offered, calls)) as sentences:
            async for sentence in sentences:
                yield sentence
        if not calls:
            return
        await self._execute_calls(calls, offered)
        if not offered:
            return
        async with aclosing(self._reply_round(system, specs, round_index + 1)) as sentences:
            async for sentence in sentences:
                yield sentence

    async def _execute_calls(self, calls: list[ToolCall], offered: list[ToolSpec]) -> None:
        results = []
        allowed = {spec.name for spec in offered}
        try:
            for call in calls:
                result = await self._dispatch(call, allowed)
                results.append(self._tool_result(call, result))
        finally:
            # Cancellation still leaves a complete protocol group for the next turn.
            results.extend(
                self._tool_result(call, {"error": "Tool execution interrupted"})
                for call in calls[len(results) :]
            )
            self._remember_many(results)

    async def _dispatch(self, call: ToolCall, allowed: set[str]) -> dict:
        if call.name not in allowed:
            return {"error": "Tools are unavailable for this response"}
        return await self.tools.execute(call)

    def _tool_result(self, call: ToolCall, result: dict) -> Message:
        payload = json.dumps(result, separators=(",", ":"), ensure_ascii=False)
        self.transcript.record(
            "tool_result", tool_call_id=call.id, text=payload, speaker_id=persona_id(self.speaker)
        )
        return Message("tool", payload, tool_call_id=call.id)

    async def _read_sentences(self, chunks, splitter, fragments, calls) -> tuple[bool, list[str]]:
        chunk = await anext(chunks, None)
        if chunk is None:
            return False, list(filter(None, [splitter.flush()]))
        if chunk.tool_call is not None:
            self.transcript.record(
                "tool_call", speaker_id=persona_id(self.speaker), **asdict(chunk.tool_call)
            )
            calls.append(chunk.tool_call)
        fragments.append(chunk.text)
        return True, splitter.feed(chunk.text)

    def _clean_sentence(self, sentence: str, first: bool) -> str:
        if first:
            return self.crew.strip_prefix(sentence, self.language)
        return sentence

    async def _stream_sentences(self, system, tools, calls, fragments) -> AsyncIterator[str]:
        prompt, messages = self.renderer.render(
            self.records, self.speaker, self.crew, self.language, self.game
        )
        pending: deque[str] = deque()
        splitter = SentenceSplitter()
        reading = True
        first = True
        async with aclosing(self.llm.stream(messages, prompt + system, tools)) as chunks:
            # Drain complete sentences before requesting another backend chunk.
            while reading or pending:
                if not pending:
                    reading, batch = await self._read_sentences(chunks, splitter, fragments, calls)
                    pending.extend(batch)
                    continue
                sentence = self._clean_sentence(pending.popleft(), first)
                first = False
                if not sentence:
                    continue
                yield sentence

    async def _stream_round(self, system: str, tools: list, calls: list) -> AsyncIterator[str]:
        fragments: list[str] = []
        completed = False
        heard = True
        try:
            source = self._stream_sentences(system, tools, calls, fragments)
            async with aclosing(source) as sentences:
                async for sentence in sentences:
                    before = self._playback_acks
                    yield sentence
                    sentence_heard = self._was_heard(before)
                    heard = heard and sentence_heard
                    self._note_heard(sentence_heard)
            completed = True
        finally:
            self._finish_round(fragments, calls, completed, heard)

    def _finish_round(self, fragments, calls, completed: bool, heard: bool) -> None:
        text = self.crew.strip_prefix("".join(fragments), self.language)
        self._log_line(text, completed and heard)
        if text or calls:
            # Defer trimming until results have been attached to their calls.
            self._record_message(
                Message("assistant", text, tool_calls=tuple(calls)), heard=completed and heard
            )
        if not calls:
            self._remember_many([])
            return
        if completed:
            return
        self._remember_many(
            [self._tool_result(call, {"error": "Response interrupted"}) for call in calls]
        )

    def _note_heard(self, heard: bool) -> None:
        if heard:
            self.crew.note_spoke(self.speaker)
