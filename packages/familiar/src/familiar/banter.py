"""Banter tuning, rolling budget, casting and strict scene parsing."""

import random
import re
from collections import deque
from itertools import permutations

from familiar.crew import Crew, all_aliases, persona_id

# Values owned by Familiar. Caps are rounded down after interpolation.
TUNING = {
    "levels": ((1, 1200.0, 2), (5, 420.0, 5), (10, 120.0, 10)),
    "hour_s": 3600,
    "quota_fraction": 0.9,
    "relationship_weight": 3.0,
    "retry_s": 30.0,
}


def limits(level: int, chattiness: float) -> tuple[float, int]:
    if not 1 <= level <= 10:
        raise ValueError("Banter level must be 1-10")
    points = TUNING["levels"]
    low, high = next(
        (a, b) for a, b in zip(points, points[1:], strict=False) if a[0] <= level <= b[0]
    )
    fraction = (level - low[0]) / (high[0] - low[0])
    gap = low[1] + fraction * (high[1] - low[1])
    cap = low[2] + fraction * (high[2] - low[2])
    return gap * (1.5 - chattiness), int(cap)


class Budget:
    def __init__(self, level: int, crew: Crew) -> None:
        mean = sum(member.chattiness for member in crew.members) / len(crew.members)
        self.gap, self.cap = limits(level, mean)
        self.scenes: deque[float] = deque()

    def available(self, now: float) -> bool:
        while self.scenes and self.scenes[0] <= now - TUNING["hour_s"]:
            self.scenes.popleft()
        return len(self.scenes) < self.cap

    def consume(self, now: float) -> None:
        self.scenes.append(now)


class Casting:
    def __init__(self) -> None:
        self.previous: tuple[str, str] | None = None

    def pick(self, crew: Crew, rng: random.Random) -> tuple[str, str]:
        pairs = [pair for pair in permutations(crew.members, 2) if self._ids(pair) != self.previous]
        initiators = list(dict.fromkeys(persona_id(a) for a, _ in pairs))
        members = {persona_id(member): member for member in crew.members}
        weights = [members[key].chattiness for key in initiators]
        first = self._choose(initiators, weights, rng)
        partners = [b for a, b in pairs if persona_id(a) == first]
        weights = [self._weight(members[first], member) for member in partners]
        second = rng.choices(partners, weights=weights, k=1)[0]
        self.previous = (first, persona_id(second))
        return self.previous

    @staticmethod
    def _choose(candidates, weights, rng):
        if not any(weights):
            return rng.choice(candidates)
        return rng.choices(candidates, weights=weights, k=1)[0]

    @staticmethod
    def _ids(pair) -> tuple[str, str]:
        return tuple(persona_id(member) for member in pair)

    @staticmethod
    def _weight(first, second) -> float:
        related = first.relationships.get(persona_id(second)) or second.relationships.get(
            persona_id(first)
        )
        if related:
            return TUNING["relationship_weight"]
        return 1.0


def parse_scene(text: str, crew: Crew, language: str) -> list[tuple[str, str]]:
    """Trim exterior prose, reject malformed interior lines and non-spoken markup."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    names = speaker_names(crew, language)
    # Only lines tagged with a crew name bound the scene, so "Here is a scene:" stays outside.
    tagged = [index for index, line in enumerate(lines) if _speaker(line, names) is not None]
    if not tagged:
        return []
    body = lines[tagged[0] : tagged[-1] + 1]
    parsed = [_parse_line(line, crew, language) for line in body]
    if any(line is None for line in parsed):
        return []
    kept = parsed[:4]
    if len(kept) < 2 or len({speaker for speaker, _ in kept}) < 2:
        return []
    return kept


# A speaker tag: id, display name or alias, then an ASCII or full-width colon.
TAG = re.compile(r"\s*([^:：]{1,40}?)\s*[:：]\s*(.*)")


def speaker_names(crew: Crew, language: str) -> dict[str, str]:
    """Every accepted tag (id, display name, aliases in any language), casefolded, to id."""
    return {
        name.casefold(): persona_id(member)
        for member in crew.members
        for name in (persona_id(member), *all_aliases(member, language))
    }


def _speaker(line: str, names: dict[str, str]) -> str | None:
    match = TAG.match(line)
    if match is None:
        return None
    return names.get(match[1].strip("*_ ").casefold())


def _parse_line(line: str, crew: Crew, language: str) -> tuple[str, str] | None:
    speaker = _speaker(line, speaker_names(crew, language))
    if speaker is None:
        return None
    match = TAG.match(line)
    text = crew.strip_prefix(match[2].strip().lstrip("*_ "), language)
    if not text or re.search(r"[\[\]*]|[<>]|\([^)]*\)", text):
        return None
    return speaker, text
