"""Spot player requests that are a crew role's job, from a data phrase list (ADR 0028)."""

import re
from functools import cache

from familiar.interfaces import load_data


@cache
def _patterns(language: str) -> tuple[tuple[str, re.Pattern], ...]:
    data = load_data("familiar", "action_phrases").value
    lists = {
        role: [phrase for phrase, on in (phrases.get(language) or {}).items() if on]
        for role, phrases in data.items()
    }
    # A role with every phrase switched off has no pattern: an empty one would match anything.
    return tuple((role, _pattern(words)) for role, words in lists.items() if words)


def _pattern(phrases: list[str]) -> re.Pattern:
    words = "|".join(
        re.escape(phrase.casefold()) for phrase in sorted(phrases, key=len, reverse=True)
    )
    return re.compile(rf"(?<!\w)(?:{words})(?!\w)")


def action_roles(text: str, language: str) -> frozenset[str]:
    """Roles whose job the request names, e.g. {"dj"} for "Ruslana, play some funk"."""
    folded = text.casefold()
    return frozenset(role for role, pattern in _patterns(language) if pattern.search(folded))
