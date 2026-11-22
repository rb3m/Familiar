"""Crew roles, addressing and weighted speaker rotation, independent of backends."""

import random
import re
import time
from collections.abc import Callable, Collection, Sequence
from itertools import chain

from familiar.interfaces import GameEvent
from familiar.persona import Persona

FOLLOW_UP_WINDOW_S = 30.0
LEAN_WEIGHT = 2.0

# Crew roles (ADR 0018): who operates the game and who runs the music. One member may hold both.
CONTROLS = "controls"
DJ = "dj"
ROLES = (CONTROLS, DJ)
# "carrier" predates the split and still means the controls holder in event data.
ROLE_ALIASES = {"carrier": CONTROLS, CONTROLS: CONTROLS, DJ: DJ}
ROLE_DUTIES = {CONTROLS: "operating the ship or game", DJ: "playing or changing music"}
# Event speaker for a turn lent to the rest of the crew in rotation (e.g. a guest music pick);
# the event's offered tools are open to that guest for the one turn.
GUEST = "guest"


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
        dj: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        follow_up_window_s: float = FOLLOW_UP_WINDOW_S,
    ) -> None:
        self.members = tuple(members)
        ids = [persona_id(member) for member in self.members]
        dj = dj or carrier
        if not 1 <= len(ids) <= 3 or len(set(ids)) != len(ids) or {carrier, dj} - set(ids):
            raise ValueError("Crew needs 1-3 unique members, a carrier and a DJ in the roster")
        by_id = dict(zip(ids, self.members, strict=True))
        self.carrier = by_id[carrier]
        self.dj = by_id[dj]
        self._guest_turns = 0
        self.previous_ambient: Persona | None = None
        self.last_speaker: Persona | None = None
        self.last_spoke_at = float("-inf")
        self.follow_up_window_s = follow_up_window_s
        self.clock = clock

    @property
    def holders(self) -> dict[str, Persona]:
        return {CONTROLS: self.carrier, DJ: self.dj}

    def holder(self, role: str) -> Persona | None:
        """The member holding a role or its alias ("carrier"); None for an unknown name."""
        return self.holders.get(ROLE_ALIASES.get(role, ""))

    def roles_of(self, member: Persona) -> frozenset[str]:
        return frozenset(role for role, holder in self.holders.items() if holder == member)

    def handoff_targets(
        self, speaker: Persona, roles: Collection[str], visited: Collection[Persona] = ()
    ) -> dict[str, Persona]:
        """Members holding a role in use the speaker lacks, minus anyone who spoke this reply."""
        excluded = {persona_id(member) for member in (speaker, *visited)}
        return {
            persona_id(holder): holder
            for role, holder in self.holders.items()
            if role in roles and persona_id(holder) not in excluded
        }

    def next_guest(self) -> Persona:
        """Members other than the DJ take turns; with nobody else aboard, the DJ."""
        guests = [member for member in self.members if member != self.dj]
        if not guests:
            return self.dj
        guest = guests[self._guest_turns % len(guests)]
        self._guest_turns += 1
        return guest

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

    def pick_for_event(self, event: GameEvent, rng: random.Random | None = None) -> Persona:
        if event.priority == "critical" or len(self.members) == 1:
            return self.carrier
        if event.data.get("speaker") == GUEST:
            return self.next_guest()
        # data["speaker"] names a role ("controls", "dj", or legacy "carrier"): its holder speaks.
        holder = self.holder(str(event.data.get("speaker", "")))
        if holder is not None:
            return holder
        rng = rng or random
        lean = self.holder(str(event.data.get("lean", "")))
        weights = [
            interest(member, event.kind)
            * getattr(member, "chattiness", 0.5)
            * (LEAN_WEIGHT if member == lean else 1.0)
            for member in self.members
        ]
        candidates = list(zip(self.members, weights, strict=True))
        others = [
            (member, weight)
            for member, weight in candidates
            if member != self.previous_ambient and weight > 0
        ]
        if others:
            candidates = others
        members, weights = zip(*candidates, strict=True)
        if not any(weights):
            weights = [1.0] * len(members)
        chosen = rng.choices(members, weights=weights, k=1)[0]
        self.previous_ambient = chosen
        return chosen

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
