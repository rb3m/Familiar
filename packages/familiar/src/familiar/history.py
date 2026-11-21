"""Shared attributed records and a replaceable per-persona prompt renderer."""

import json
import re
from dataclasses import dataclass
from typing import Protocol

from familiar.crew import ROLE_DUTIES, ROLES, Crew, persona_id
from familiar.interfaces import Message, ToolCall
from familiar.persona import Persona

HISTORY_LABELS = ("Tool outcome", "Cut off; not heard in full")
_LABELLED_SENTENCE = re.compile(
    r"(?:^|(?<=[.!?…。！？])|(?<=\n))\s*(?:[^\n:：\[.!?]+[:：]\s*)?\[(?:"
    + "|".join(re.escape(label) for label in HISTORY_LABELS)
    + r")\][\s\S]*?(?:[.!?…。！？]+(?=\s|$)|\n|$)",
    re.IGNORECASE,
)


def strip_history_labels(text: str) -> str:
    """Remove leaked labelled sentences/lines while preserving ordinary spoken text."""
    cleaned, count = _LABELLED_SENTENCE.subn(
        lambda match: "\n" if match.group().endswith("\n") else "", text
    )
    if count:
        return cleaned.strip()
    return text


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
        system += self._duties(speaker, crew)
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

    @staticmethod
    def _duties(speaker: Persona, crew: Crew) -> str:
        """Who does which job, for a speaker lacking a role (ADR 0018)."""
        others = [role for role in ROLES if crew.holders[role] != speaker]
        if not others:
            return ""
        jobs = " ".join(
            f"Only {crew.holders[role].display_name} handles {ROLE_DUTIES[role]}."
            for role in others
        )
        return (
            f"\n{jobs} When the player asks for one of those, call hand_off when offered, "
            "with an optional short in-character remark, after doing your own part. "
            "Do not claim to do it yourself. Never say a tool's name aloud."
        )

    def _own_history(self, system: str, records: list[HistoryRecord]) -> tuple[str, list[Message]]:
        cut_off = [
            record.text
            for record in records
            if record.kind in {"assistant", "banter"} and not record.heard and record.text
        ]
        if cut_off:
            system += "\n\nCut-off generations (not said in full):\n" + "\n".join(cut_off)
        messages = [
            self._message(record)
            for record in records
            if record.heard or record.kind not in {"assistant", "banter"} or record.tool_calls
        ]
        return system, messages

    def chronological(self, records: list[HistoryRecord], crew: Crew) -> str:
        """Reuse the same labelled, compact-outcome transcript for scene prompts."""
        lines = [self._line(record, None, crew) for record in records]
        return "\n".join(line for line in lines if line)

    def roster(self, crew: Crew, game: str | None) -> str:
        return "\n".join(self._scene_member(member, game) for member in crew.members)

    def _scene_member(self, member: Persona, game: str | None) -> str:
        relationships = "; ".join(
            f"toward {key}: {value}" for key, value in member.relationships.items()
        )
        return (
            self._roster_line(member, member, game)
            + f" Truthfulness: {member.truthfulness}. {relationships}\n"
            + f'Character notes for {member.display_name} (written to her as "you"):\n'
            + member.prompt
            + "\n"
            + "\n".join(member.style_rules)
            + "\n"
            + member.game_hooks.get(game, "")
        )

    @staticmethod
    def _roster_line(member: Persona, speaker: Persona, game: str | None) -> str:
        role = getattr(member, "roles", {}).get(game, "crew member")
        relationship = getattr(speaker, "relationships", {}).get(persona_id(member), "")
        return f"- {member.display_name} ({persona_id(member)}): {role} {relationship}".rstrip()

    @staticmethod
    def _message(record: HistoryRecord) -> Message:
        message = record.message()
        if record.heard or record.kind not in {"assistant", "banter"}:
            return message
        return Message(
            message.role,
            "[Cut off; not heard in full]",
            message.tool_calls,
            message.tool_call_id,
        )

    @staticmethod
    def _line(record: HistoryRecord, speaker: Persona | None, crew: Crew) -> str:
        names = {persona_id(member): member.display_name for member in crew.members}
        names["player"] = "Player"
        if record.speaker_id not in names or record.kind == "game_event":
            return ""
        name = names[record.speaker_id]
        if speaker is not None and record.speaker_id == persona_id(speaker):
            name = f"{name} (you)"
        if record.kind == "tool_result":
            return f"{name}: [Tool outcome] {outcome(record.text)}"
        if not record.heard:
            return f"{name}: [Cut off; not heard in full] {record.text}"
        return f"{name}: {record.text}"
