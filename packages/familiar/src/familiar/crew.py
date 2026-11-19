"""Roster order and deterministic speaker selection, independent of backends."""

import random
import re
import time
from collections.abc import Callable, Sequence
from itertools import chain

from familiar.interfaces import GameEvent
from familiar.persona import Persona

FOLLOW_UP_WINDOW_S = 30.0


def persona_id(persona: Persona) -> str:
    identifier = getattr(persona, "id", None)
    if isinstance(identifier, str):
        return identifier
    return str(getattr(persona, "display_name", "companion")).lower()


def aliases(persona: Persona, language: str) -> list[str]:
    name = str(getattr(persona, "display_name", persona_id(persona)))
    configured = getattr(persona, "aliases", {})
    if not isinstance(configured, dict):
        return [name]
    return [name, *configured.get(language, [])]


def all_aliases(persona: Persona, language: str) -> list[str]:
    configured = getattr(persona, "aliases", {})
    if not isinstance(configured, dict):
        return aliases(persona, language)
    return [*aliases(persona, language), *chain.from_iterable(configured.values())]


def interest(persona: Persona, kind: str) -> float:
    weights = getattr(persona, "interests", {})
    if kind in weights:
        return weights[kind]
    prefix = max((key for key in weights if kind.startswith(key)), key=len, default=None)
    if prefix is not None:
        return weights[prefix]
    return 1.0


def name_pattern(names: Sequence[str]) -> str:
    return "(?:" + "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True)) + ")"


class Crew:
    def __init__(
        self,
        members: Sequence[Persona],
        carrier: str,
        clock: Callable[[], float] = time.monotonic,
        follow_up_window_s: float = FOLLOW_UP_WINDOW_S,
    ) -> None:
        self.members = tuple(members)
        ids = [persona_id(member) for member in self.members]
        if not 1 <= len(ids) <= 3 or len(set(ids)) != len(ids) or carrier not in ids:
            raise ValueError("Crew needs 1-3 unique members and a carrier in the roster")
        self.carrier = next(member for member in self.members if persona_id(member) == carrier)
        self.last_speaker: Persona | None = None
        self.last_spoke_at = float("-inf")
        self.follow_up_window_s = follow_up_window_s
        self.clock = clock

    def note_spoke(self, speaker: Persona) -> None:
        self.last_speaker = speaker
        self.last_spoke_at = self.clock()

    def pick_for_utterance(self, text: str, language: str) -> Persona:
        matches = [
            (match.start(), index, member)
            for index, member in enumerate(self.members)
            if (
                match := re.search(
                    r"(?<!\w)" + name_pattern(aliases(member, language)) + r"(?!\w)", text, re.I
                )
            )
        ]
        if matches:
            return min(matches, key=lambda item: item[:2])[2]
        if self.last_speaker is not None and (
            self.clock() - self.last_spoke_at <= self.follow_up_window_s
        ):
            return self.last_speaker
        return self.carrier

    def pick_for_event(self, event: GameEvent) -> Persona:
        # data["speaker"] == "carrier": the event is a job for whoever performs actions.
        carrier = event.priority == "critical" or event.data.get("speaker") == "carrier"
        if carrier or len(self.members) == 1:
            return self.carrier
        return max(
            self.members,
            key=lambda member: (
                interest(member, event.kind) * getattr(member, "chattiness", 0.5),
                member == self.carrier,
            ),
        )

    def pick_idle(self, rng: random.Random) -> Persona:
        candidates = [member for member in self.members if member != self.last_speaker]
        if not candidates:
            return self.carrier
        weights = [getattr(member, "chattiness", 0.5) for member in candidates]
        if not any(weights):
            return candidates[0]
        return rng.choices(candidates, weights=weights, k=1)[0]

    def strip_prefix(self, text: str, language: str) -> str:
        names = list(chain.from_iterable(all_aliases(member, language) for member in self.members))
        pattern = r"^\s*" + name_pattern(names) + r"\s*[:：]\s*"
        return re.sub(pattern, "", text, count=1, flags=re.I)
