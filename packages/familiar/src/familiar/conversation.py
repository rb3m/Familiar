"""Coordinate streamed replies, bounded history, and transcript events."""

import json
import logging
import random
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Collection
from contextlib import aclosing
from dataclasses import asdict, replace
from functools import partial

from familiar.alerts import AlertKeeper
from familiar.banter import (
    TUNING,
    Casting,
    content_words,
    near_duplicate,
    opening_avoidance,
    parse_scene,
)
from familiar.crew import GUEST, RELAY_ROLES, ROLE_DUTIES, ROLES, Crew, persona_id
from familiar.fallback import llm_budget_blocked
from familiar.flavor import FlavorKeeper, flavor_hint
from familiar.hands import ActionKeeper
from familiar.history import (
    DefaultHistoryRenderer,
    HistoryRecord,
    HistoryRenderer,
    strip_history_labels,
)
from familiar.interfaces import (
    REQUIRE_TOOL,
    GameEvent,
    LLMProvider,
    Message,
    SceneContext,
    SceneTopic,
    TextToSpeech,
    ToolCall,
    ToolSpec,
)
from familiar.memory import MemoryKeeper
from familiar.memory.keeper import CREW_REPLY_WINDOW
from familiar.persona import Persona
from familiar.pipeline.sentences import SentenceSplitter
from familiar.speech import MARKUP_RULE
from familiar.tools import ToolRegistry, validate_arguments
from familiar.topics import TopicPicker
from familiar.transcript import TranscriptLog

# Optional high-salience facts the model latches onto: named only when they are the topic.
HIDDEN_RULES = {
    "location": "Do not mention the city or its nickname.",
    "song": "Do not mention the current song or artist.",
}

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


HANDOFF = "hand_off"


def handoff_spec(targets: dict[str, Persona], crew: Crew) -> ToolSpec:
    """Hand the request to a crew member holding a role the speaker lacks (ADR 0013, 0018)."""
    duties = "; ".join(
        f"{identifier} = {member.display_name}, "
        + " and ".join(ROLE_DUTIES[role] for role in ROLES if role in crew.roles_of(member))
        for identifier, member in targets.items()
    )
    required = ["to"] if len(targets) > 1 else []
    return ToolSpec(
        HANDOFF,
        f"Hand the player's request to the crew member who does it, in this reply ({duties}).",
        {
            "type": "object",
            "properties": {
                "to": {"type": "string", "enum": list(targets), "description": "Crew member id."},
                "line": {
                    "type": "string",
                    "description": "Optional in-character spoken remark (at most 200 characters).",
                    "maxLength": 200,
                },
            },
            "required": required,
            "additionalProperties": False,
        },
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
        scene_context: Callable[[], SceneContext] | None = None,
        clock: Callable[[], float] = time.monotonic,
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
        self.scene_context = scene_context
        self.clock = clock
        self.picker = TopicPicker(clock, self.rng)
        self._recent_lines: deque[tuple[float, frozenset[str]]] = deque(maxlen=1000)
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
        if message.role == "assistant" and heard:
            self._note_line(message.text)
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
        del self.records[: self._trim_start()]

    def _trim_start(self) -> int:
        """Cut only at a user turn: Gemini rejects history opening with a call or result."""
        limit = len(self.records) - self.history_limit
        if limit <= 0:
            return 0
        users = [i for i, record in enumerate(self.records) if record.message().role == "user"]
        # Prefer the first user turn within the limit; else keep the whole current turn.
        current = max((i for i in users if i < limit), default=0)
        return min((i for i in users if i >= limit), default=current)

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
        self.picker.note_event(event.kind)
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
        tastes = getattr(self.speaker, "tastes", None)
        taste = tastes.get(event.data.get("taste", "")) if isinstance(tastes, dict) else None
        if taste:
            text += "\nYour taste (hints, not rules): " + "; ".join(taste)
        candidate = self.flavor.choose(event) if self.flavor is not None else None
        if candidate is not None:
            text += flavor_hint(candidate)
        avoidance = opening_avoidance(self.records, persona_id(self.speaker))
        if avoidance:
            text += "\n" + avoidance
        recorded = False
        offered = tuple(event.data.get("tools", ()))
        granted = event.data.get("speaker") == GUEST
        turn = self._turn(text, offered=offered, granted=granted)
        async with aclosing(turn) as sentences:
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
        scene = self.scene_context() if self.scene_context is not None else None
        lines = await self._generate_scene(pair, scene)
        if not lines:
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
            for record in filter(lambda record: record.heard, records):
                self._note_line(record.text)
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

    def _note_line(self, text: str) -> None:
        self._prune_lines()
        now = self.clock()
        self._recent_lines.extend(
            (now, content_words(line)) for line in text.splitlines() if line.strip()
        )

    def _prune_lines(self) -> None:
        cutoff = self.clock() - TUNING["duplicate_window_s"]
        while self._recent_lines and self._recent_lines[0][0] <= cutoff:
            self._recent_lines.popleft()

    def _repeats(self, lines: list[tuple[str, str]]) -> bool:
        self._prune_lines()
        candidates = [content_words(text) for _, text in lines]
        return any(
            near_duplicate(candidate, previous)
            for candidate in candidates
            for _, previous in self._recent_lines
        )

    def _scene_topics(self, scene: SceneContext, pair=()) -> tuple[SceneTopic, ...]:
        """Shared topics plus the speaking pair's own (all members' when no pair is given)."""
        speakers = set(pair) or {persona_id(member) for member in self.crew.members}
        personal = tuple(
            SceneTopic(
                "persona",
                f"{persona_id(member)}:{text}",
                f"{member.display_name} likes talking about {text}",
            )
            for member in self.crew.members
            if persona_id(member) in speakers
            for text in getattr(member, "topics", ())
        )
        return (*scene.topics, *personal)

    async def _generate_scene(self, pair, scene: SceneContext | None) -> list[tuple[str, str]]:
        excluded = []
        for _ in range(2):
            topic = self._pick_scene_topic(scene, excluded, pair)
            if scene is not None and topic is None:
                return []
            excluded.append(topic)
            lines = await self._scene_attempt(pair, scene, topic)
            if not self._repeats(lines):
                return lines
            logging.getLogger(__name__).debug("Dropping near-duplicate banter scene")
        return []

    def _pick_scene_topic(self, scene, excluded, pair=()) -> SceneTopic | None:
        if scene is None:
            return None
        return self.picker.pick(self._scene_topics(scene, pair), exclude=excluded)

    def _scene_background(self, scene: SceneContext | None, topic: SceneTopic | None) -> str:
        if scene is None:
            situation = self.context() if self.context is not None else "desk mode"
            return "Current situation: " + situation
        lines = [f"Current situation: {scene.situation}"] if scene.situation else []
        if scene.frame:
            lines.append(f"Background (do not recite): {scene.frame}")
        lines.append(f"Topic for this scene: {topic.text}")
        hidden = {item.family for item in scene.topics} - {topic.family}
        lines.extend(HIDDEN_RULES[family] for family in sorted(hidden & HIDDEN_RULES.keys()))
        return "\n".join(lines)

    async def _scene_attempt(self, pair, scene, topic) -> list[tuple[str, str]]:
        renderer = DefaultHistoryRenderer()
        situation = self._scene_background(scene, topic)
        prompt = (
            f"Write a short crew banter scene in session language {self.language}.\n"
            f"Initiator: {pair[0]}; partner: {pair[1]}. Both must speak. "
            "A third crew member may join for at most one line.\n"
            "Output ONLY 2-4 short spoken lines, aiming for 2-3, each as "
            "<persona_id>: <text>, with actual IDs and no angle brackets. "
            "No stage directions, player lines or claims to take game actions. " + MARKUP_RULE + " "
            "Strict members use only supplied facts. Playful members may joke but must "
            "never misreport game state. Practical information must always be exact.\n"
            "Crew roster (third-person roles and relationships):\n"
            + renderer.roster(self.crew, self.game)
            + "\n"
            + situation
            + "\nRecent chronological transcript (unheard lines were NOT said):\n"
            + renderer.chronological(self.records[-TUNING["scene_transcript_records"] :], self.crew)
        )
        avoidance = [
            f"{persona_id(member)}: {hint}"
            for member in self.crew.members
            if (hint := opening_avoidance(self.records, persona_id(member)))
        ]
        prompt += "\n" + "\n".join(avoidance)
        if self.memory is not None:
            prompt += self.memory.crew_callback()
        async with aclosing(
            self.llm.stream([Message("user", "Write the scene.")], prompt, [])
        ) as source:
            chunks = [chunk async for chunk in source]
        if any(chunk.tool_call is not None for chunk in chunks):
            logging.getLogger(__name__).debug("Dropping banter: unexpected tool call")
            return []
        lines = parse_scene(
            strip_history_labels("".join(chunk.text for chunk in chunks)), self.crew, self.language
        )
        speakers = {speaker for speaker, _ in lines}
        extras = [speaker for speaker, _ in lines if speaker not in pair]
        if not lines or not set(pair).issubset(speakers) or len(extras) > 1:
            logging.getLogger(__name__).debug("Dropping malformed banter scene")
            return []
        return lines

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
        return self.crew.pick_for_event(event, self.rng)

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

    async def _turn(
        self,
        text: str,
        player_turn: bool = False,
        offered: tuple[str, ...] = (),
        granted: bool = False,
    ) -> AsyncIterator[str]:
        kinds = {True: "player", False: "game_event"}
        self._record_message(Message("user", text), kind=kinds[player_turn])
        self._remember_many([])
        reply = self._respond(text, player_turn, offered, granted=granted)
        async with aclosing(reply) as sentences:
            async for sentence in sentences:
                yield sentence

    def _handoff_targets(self, visited: tuple[Persona, ...]) -> dict[str, Persona]:
        return self.crew.handoff_targets(self.speaker, self.tools.roles_in_use, visited)

    def _turn_context(self, player_turn: bool) -> str:
        if not player_turn and self.scene_context is not None:
            scene = self.scene_context()
            return "\n".join(filter(None, (scene.situation, scene.frame)))
        return self.context() if self.context is not None else ""

    async def _respond(
        self,
        text: str,
        player_turn: bool,
        offered: tuple[str, ...] = (),
        visited: tuple[Persona, ...] = (),
        granted: bool = False,
    ) -> AsyncIterator[str]:
        """Respond to the current request without recording another player turn.

        ``visited`` lists crew who already spoke in this reply; hand-offs never go back to them.
        ``granted`` opens the event's offered autonomous tools whatever role owns them (a guest
        turn, ADR 0018).
        """
        system = ""
        context = self._turn_context(player_turn)
        if context:
            system += "\n\nCurrent situation (exact, from live telemetry):\n" + context
        memories = ""
        if player_turn and self.memory is not None:
            memories = self.memory.context(text)
        if memories:
            system += "\n\n" + memories
        roles = self.crew.roles_of(self.speaker)
        specs = self.tools.offered(offered, None if granted else roles)
        if player_turn:
            specs = self.tools.specs(roles)
        targets = self._handoff_targets(visited)
        if player_turn and targets:
            specs.append(handoff_spec(targets, self.crew))
            specs.extend(self._relay_specs(specs, visited))
        if any(spec.name == "set_alert" for spec in specs):
            system += ALERT_RULE
        if any(spec.name.startswith("act_") for spec in specs):
            system += ACTION_RULE
        # A hand-off target was given this because it is her job: she acts, then speaks
        # (ADR 0021). Without this, chatty personas describe the action instead of calling it.
        required = bool(visited) and player_turn and any(spec.name != HANDOFF for spec in specs)
        reply = self._reply_round(
            system, specs, request=text, visited=visited, require_tool=required
        )
        async with aclosing(reply) as sentences:
            async for sentence in sentences:
                yield sentence

    async def _reply_round(
        self,
        system: str,
        specs: list[ToolSpec],
        round_index: int = 0,
        request: str = "",
        visited: tuple[Persona, ...] = (),
        require_tool: bool = False,
    ) -> AsyncIterator[str]:
        calls: list[ToolCall] = []
        offered = []
        if round_index < 3:
            offered = specs
        # Passing the job on (hand_off) counts as acting, so chains still work.
        required = require_tool and round_index == 0
        async with aclosing(self._stream_round(system, offered, calls, required)) as sentences:
            async for sentence in sentences:
                yield sentence
        if not calls:
            return
        handoff = next((call for call in calls if call.name == HANDOFF), None)
        if not any(spec.name == HANDOFF for spec in offered):
            handoff = None  # a hallucinated hand-off is dispatched, and refused, like any tool
        target = self._handoff_target(handoff, offered, visited)
        relayed, relay_target = self._relayed(calls, offered, visited)
        if handoff is None and relay_target is not None:
            # The speaker used the job's own tool: pass the job on instead of running it.
            handoff, target = relayed, relay_target
        decide = partial(self._handoff_result, target)
        blocked = self._relay_names(offered)
        outcome = await self._execute_calls(calls, offered, handoff, decide, blocked)
        if target is not None and outcome.get("status") == "handed_off":
            async with aclosing(self._hand_off(handoff, target, request, visited)) as sentences:
                async for sentence in sentences:
                    yield sentence
            return
        if not offered:
            return
        async with aclosing(
            self._reply_round(system, specs, round_index + 1, request, visited)
        ) as sentences:
            async for sentence in sentences:
                yield sentence

    def _handoff_target(
        self, handoff: ToolCall | None, offered: list[ToolSpec], visited: tuple[Persona, ...]
    ) -> Persona | None:
        """The validated target, or None: a bad call becomes an error, never a guess."""
        if handoff is None:
            return None
        spec = next(spec for spec in offered if spec.name == HANDOFF)
        try:
            validate_arguments(handoff.arguments, spec.parameters)
        except ValueError:
            return None
        targets = self._handoff_targets(visited)
        if "to" not in handoff.arguments and len(targets) == 1:
            return next(iter(targets.values()))
        return targets.get(handoff.arguments.get("to"))

    def _relay_specs(self, specs: list[ToolSpec], visited) -> list[ToolSpec]:
        """Other role holders' tools, offered as the act of passing the job on (ADR 0027)."""
        names = {spec.name for spec in specs}
        targets = self._handoff_targets(visited).values()
        return [
            replace(
                spec,
                description=f"Passes this request to {holder.display_name}, who does it: "
                + spec.description,
            )
            for spec in self.tools.specs(None)
            if spec.name not in names
            and self.tools.role_of(spec.name) in RELAY_ROLES
            and (holder := self.crew.holder(self.tools.role_of(spec.name) or "")) in targets
        ]

    def _relay_names(self, offered: list[ToolSpec]) -> set[str]:
        """Offered tools the speaker may only pass on: other roles' tools beside hand_off."""
        names = {spec.name for spec in offered}
        if HANDOFF not in names:
            return set()
        own = {spec.name for spec in self.tools.specs(self.crew.roles_of(self.speaker))}
        return names - own - {HANDOFF}

    def _relayed(self, calls, offered, visited) -> tuple[ToolCall | None, Persona | None]:
        """The first call to a relay tool and the role holder it passes the job to."""
        relays = self._relay_names(offered)
        targets = list(self._handoff_targets(visited).values())
        for call in calls:
            holder = self.crew.holder(self.tools.role_of(call.name) or "")
            if call.name in relays and holder in targets:
                return call, holder
        return None, None

    def _handoff_result(self, target: Persona | None, own: list[dict]) -> dict:
        """Hand off only once the speaker's own part needs nothing more from the player."""
        if target is None:
            targets = ", ".join(self._handoff_targets(()))
            return {"error": f"Not handed off: set 'to' to one of: {targets}"}
        if any(result.get("status") == "needs_confirmation" for result in own):
            return {
                "error": "Not handed off: ask the player to confirm the pending action first; "
                "hand off after they answer."
            }
        return {"status": "handed_off", "detail": target.display_name}

    async def _hand_off(
        self, handoff: ToolCall, target: Persona, request: str, visited: tuple[Persona, ...]
    ) -> AsyncIterator[str]:
        # The speaker's own tools in this round already ran ("gear down and play something": the
        # controls holder lowers the gear, then the DJ picks the music). The target sees the
        # outcomes, and can hand on only to crew who have not spoken in this reply.
        line = handoff.arguments.get("line", "") if handoff.name == HANDOFF else ""
        async with aclosing(self._handoff_line(line)) as sentences:
            async for sentence in sentences:
                yield sentence
        visited = (*visited, self.speaker)
        self.speaker = target
        async with aclosing(self._respond(request, True, visited=visited)) as sentences:
            async for sentence in sentences:
                yield sentence

    async def _handoff_line(self, line: object) -> AsyncIterator[str]:
        if not isinstance(line, str):
            return
        line = strip_history_labels(self.crew.strip_prefix(line[:200], self.language))
        if not line:
            return
        before = self._playback_acks
        heard = False
        try:
            yield line
            heard = self._was_heard(before)
            self._note_heard(heard)
        finally:
            self._record_message(Message("assistant", line), heard=heard)
            self._log_line(line, heard)

    async def _execute_calls(
        self,
        calls: list[ToolCall],
        offered: list[ToolSpec],
        held: ToolCall | None = None,
        decide: Callable[[list[dict]], dict] | None = None,
        blocked: Collection[str] = (),
    ) -> dict:
        """Run calls in order; ``held`` is answered last by ``decide`` from the others' results.

        Returns the held call's result (empty without one).
        """
        outcomes: dict[int, dict] = {}
        allowed = {spec.name for spec in offered} - {HANDOFF} - set(blocked)
        try:
            for index, call in enumerate(calls):
                if call is held:
                    continue
                outcomes[index] = await self._dispatch(call, allowed)
            held_index = next((i for i, call in enumerate(calls) if call is held), None)
            if held_index is not None and decide is not None:
                outcomes[held_index] = decide(list(outcomes.values()))
        finally:
            # Cancellation still leaves a complete protocol group for the next turn.
            interrupted = {"error": "Tool execution interrupted"}
            self._remember_many(
                [
                    self._tool_result(call, outcomes.get(index, interrupted))
                    for index, call in enumerate(calls)
                ]
            )
        return next((outcomes[i] for i, call in enumerate(calls) if call is held), {})

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
            sentence = self.crew.strip_prefix(sentence, self.language)
        return strip_history_labels(sentence)

    def _stream(self, messages, system: str, tools: list, require_tool: bool):
        if require_tool and tools and REQUIRE_TOOL in getattr(self.llm, "capabilities", ()):
            return self.llm.stream(messages, system, tools, require_tool=True)
        return self.llm.stream(messages, system, tools)

    async def _stream_sentences(
        self, system, tools, calls, fragments, require_tool: bool = False
    ) -> AsyncIterator[str]:
        prompt, messages = self.renderer.render(
            self.records, self.speaker, self.crew, self.language, self.game
        )
        pending: deque[str] = deque()
        splitter = SentenceSplitter()
        reading = True
        first = True
        source = self._stream(messages, prompt + system, tools, require_tool)
        async with aclosing(source) as chunks:
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

    async def _stream_round(
        self, system: str, tools: list, calls: list, require_tool: bool = False
    ) -> AsyncIterator[str]:
        fragments: list[str] = []
        completed = False
        heard = True
        try:
            source = self._stream_sentences(system, tools, calls, fragments, require_tool)
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
        text = strip_history_labels(self.crew.strip_prefix("".join(fragments), self.language))
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
