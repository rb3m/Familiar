"""What the voice says: the text minus stage directions. Transcripts keep the raw text."""

import re
from functools import cache
from typing import Any

from familiar.interfaces import load_data

AUDIO_TAGS = "audio_tags"  # TTS capability: acts out [sighs]-style tags (ElevenLabs v3)

# A stage note or audio tag: lowercase words only. "[Track 2]" or "[Intro]" is spoken text.
TAG = re.compile(r"\[([a-z]+(?: [a-z]+)*)\]")
# A tool call written out as text instead of made: "[play_music(query='rock')]",
# "{play_music: rock}", "[Tool call: play_…". Never spoken (ADR 0021).
CALL_START = re.compile(r"[\[{]\s*(?:tool[ _-]?call\b|[a-z]+(?:_[a-z]+)+\s*[(:])", re.IGNORECASE)
CLOSERS = {"[": "]", "{": "}", "(": ")"}
SPAN = re.compile(r"\*{1,2}([^*]+?)\*{1,2}")
WORD = re.compile(r"[^\W\d_]+")
# Left over after a removed direction: "[sighs]. Oh" -> "Oh"; a minus before a digit stays.
ORPHAN_LEAD = re.compile(r"^(?:[\s.,;:!?…]|[—–-](?!\d))+")
SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?…])")
REPEATED_PUNCT = re.compile(r"([,;:])\s*(?=[,.;:!?…])")

# Tested live (ADR 0021): asking for [audio tags], with or without an example, made Gemini
# answer in prose instead of calling tools. Ask for tags only once a tag-capable voice exists.
MARKUP_RULE = (
    "Write only words you say aloud: no stage directions, actions, gestures, sound effects or "
    "bracketed notes. Asterisks only mark emphasis on spoken words."
)


def inflections(verb: str) -> set[str]:
    """sigh -> sighs, sighed, sighing; smile -> smiling; nod -> nodding (not "nodes")."""
    plural = "es" if re.search(r"(s|sh|ch|x|z)$", verb) else "s"
    forms = {verb + plural, verb + "ing"}
    if verb.endswith("e"):
        forms |= {verb + "d", verb[:-1] + "ing"}
    if not verb.endswith("e"):
        forms.add(verb + "ed")
    if re.search(r"[^aeiou][aeiou][bdgmnpt]$", verb):
        forms |= {verb + verb[-1] + "ed", verb + verb[-1] + "ing"}
    return forms


@cache
def _vocabulary() -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    data = load_data("familiar", "speech").value
    words = set()
    for verb in data["direction_verbs"]:
        words |= {verb, *inflections(verb)}
    for verb in data["direction_verbs_inflected"]:
        words |= inflections(verb)
    return frozenset(data["vocal_tags"]), frozenset(words), frozenset(data["direction_lead_words"])


def is_direction(span: str) -> bool:
    """An asterisk span narrating an action ("Sighs, smoke curling…") rather than emphasis."""
    _, actions, leads = _vocabulary()
    words = [word.casefold() for word in WORD.findall(span)]
    first = next((word for word in words if word not in leads), None)
    return first in actions


def performs_tags(backend: Any) -> bool:
    return AUDIO_TAGS in getattr(backend, "capabilities", frozenset())


def speakable(text: str, backend: Any = None) -> str:
    """Drop directions and unperformable tags, unwrap emphasis, tidy what is left."""
    vocal, _, _ = _vocabulary()
    keep_tags = performs_tags(backend)

    def span(match: re.Match) -> str:
        return "" if is_direction(match[1]) else match[1]

    def tag(match: re.Match) -> str:
        return match[0] if keep_tags and match[1] in vocal else ""

    without_calls = strip_calls(text)
    spoken = TAG.sub(tag, SPAN.sub(span, without_calls))
    removed = without_calls != text or spoken != SPAN.sub(lambda match: match[1], text)
    spoken = re.sub(r"\s{2,}", " ", spoken.replace("\n", " "))
    if removed:
        spoken = REPEATED_PUNCT.sub("", SPACE_BEFORE_PUNCT.sub(r"\1", spoken))
        spoken = ORPHAN_LEAD.sub("", spoken.replace("*", ""))
    return spoken.strip()


def _call_end(text: str, start: int) -> int:
    """Index after the bracket opened at ``start``, through nesting and quotes; else the end."""
    expected, quote = [], None
    for index in range(start, len(text)):
        char = text[index]
        if quote is not None:
            quote = None if char == quote else quote
            continue
        if char in "'\"":
            quote = char
            continue
        if char in CLOSERS:
            expected.append(CLOSERS[char])
            continue
        if expected and char == expected[-1]:
            expected.pop()
            if not expected:
                return index + 1
    return len(text)


def strip_calls(text: str) -> str:
    while (match := CALL_START.search(text)) is not None:
        text = text[: match.start()] + text[_call_end(text, match.start()) :]
    return text


def looks_like_call(text: str) -> bool:
    return CALL_START.search(text) is not None


def open_markup(text: str) -> bool:
    """An unfinished *span* or [tag], so a sentence break would cut a direction in two."""
    rest = TAG.sub("", SPAN.sub("", text))
    return "*" in rest or rest.rfind("[") > rest.rfind("]")
