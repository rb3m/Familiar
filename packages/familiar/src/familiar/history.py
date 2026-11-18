"""Shared attributed records and a replaceable per-persona prompt renderer."""

import json
from dataclasses import dataclass
from typing import Protocol

from familiar.crew import Crew, persona_id
from familiar.interfaces import Message, ToolCall
from familiar.persona import Persona


@dataclass(frozen=True)
class HistoryRecord:
    speaker_id: str
    kind: str
    text: str
    heard: bool = True
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None

    def message(self) -> Message:
        roles = {"player": "user", "game_event": "user", "tool_result": "tool"}
        return Message(
            roles.get(self.kind, "assistant"), self.text, self.tool_calls, self.tool_call_id
        )


def outcome(text: str) -> str:
    """Expose a bounded status/detail, never another speaker's raw payload."""
    try:
        result = json.loads(text)
    except ValueError:
        return "Tool completed; no outcome available"
    fields = [str(result[key]) for key in ("status", "detail", "error") if key in result]
    return " ".join("; ".join(fields).split())[:240] or "Tool completed"


class HistoryRenderer(Protocol):
    def render(
        self,
        records: list[HistoryRecord],
        speaker: Persona,
        crew: Crew,
        language: str,
        game: str | None,
    ) -> tuple[str, list[Message]]: ...


class DefaultHistoryRenderer:
    def render(
        self,
        records: list[HistoryRecord],
        speaker: Persona,
        crew: Crew,
        language: str,
        game: str | None,
    ) -> tuple[str, list[Message]]:
        system = speaker.system_prompt(language, game)
        if len(crew.members) == 1:
            return self._own_history(system, records)
        roster = [self._roster_line(member, speaker, game) for member in crew.members]
        system += "\n\nCrew roster:\n" + "\n".join(roster)
        system += (
            f"\nYou are {speaker.display_name}; speak only as {speaker.display_name}; "
            "never write other crew members' lines; no name prefix."
        )
        if speaker != crew.carrier:
            system += (
                f"\nOnly {crew.carrier.display_name} carries out mutating game actions. "
                "If asked to act, answer in character and name "
                f"{crew.carrier.display_name}. Do not claim to act or silently delegate."
            )
        # Chronological, every speaker labelled, so replies keep their order relative to the
        # player's turns; the persona's own lines also stay as model turns below.
        has_others = any(
            record.speaker_id not in {"player", persona_id(speaker)} for record in records
        )
        transcript = [self._line(record, speaker, crew) for record in records]
        if has_others:
            system += (
                "\n\nConversation so far, oldest first (context only; do not continue it as "
                "anyone but yourself):\n" + "\n".join(line for line in transcript if line)
            )
        own = [record for record in records if record.speaker_id in {"player", persona_id(speaker)}]
        return self._own_history(system, own)

    def _own_history(self, system: str, records: list[HistoryRecord]) -> tuple[str, list[Message]]:
        cut_off = [
            record.text
            for record in records
            if record.kind == "assistant" and not record.heard and record.text
        ]
        if cut_off:
            system += "\n\nCut-off generations (not said in full):\n" + "\n".join(cut_off)
        messages = [
            self._message(record)
            for record in records
            if record.heard or record.kind != "assistant" or record.tool_calls
        ]
        return system, messages

    @staticmethod
    def _roster_line(member: Persona, speaker: Persona, game: str | None) -> str:
        role = getattr(member, "roles", {}).get(game, "crew member")
        relationship = getattr(speaker, "relationships", {}).get(persona_id(member), "")
        return f"- {member.display_name} ({persona_id(member)}): {role} {relationship}".rstrip()

    @staticmethod
    def _message(record: HistoryRecord) -> Message:
        message = record.message()
        if record.heard or record.kind != "assistant":
            return message
        return Message(
            message.role,
            "[Cut off; not heard in full]",
            message.tool_calls,
            message.tool_call_id,
        )

    @staticmethod
    def _line(record: HistoryRecord, speaker: Persona, crew: Crew) -> str:
        names = {persona_id(member): member.display_name for member in crew.members}
        names["player"] = "Player"
        if record.speaker_id not in names or record.kind == "game_event":
            return ""
        name = names[record.speaker_id]
        if record.speaker_id == persona_id(speaker):
            name = f"{name} (you)"
        if record.kind == "tool_result":
            return f"{name}: [Tool outcome] {outcome(record.text)}"
        if not record.heard:
            return f"{name}: [Cut off; not heard in full] {record.text}"
        return f"{name}: {record.text}"
