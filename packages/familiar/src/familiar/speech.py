"""What the voice says: the text minus stage directions. Transcripts keep the raw text."""

import re
from functools import cache
from typing import Any

from familiar.interfaces import load_data

AUDIO_TAGS = "audio_tags"  # TTS capability: acts out [sighs]-style tags (ElevenLabs v3)

# A reserved tag: lowercase words, at most three. "[Track 2]" or "[Intro]" is spoken text.
TAG = re.compile(r"\[([a-z]+(?: [a-z]+){0,2})\]")
SPAN = re.compile(r"\*{1,2}([^*]+?)\*{1,2}")
WORD = re.compile(r"[^\W\d_]+")
# Left over after a removed direction: "[sighs]. Oh" -> "Oh"; a minus before a digit stays.
ORPHAN_LEAD = re.compile(r"^(?:[\s.,;:!?…]|[—–-](?!\d))+")
SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?…])")
REPEATED_PUNCT = re.compile(r"([,;:])\s*(?=[,.;:!?…])")

MARKUP_RULE = (
    "Formatting for the voice: write a vocal sound only as a short lowercase audio tag in "
    "square brackets, such as [sighs], [laughs softly] or [whispers]; never narrate actions, "
    "gestures or scenery. Asterisks are only for spoken emphasis on words you say, such as "
    "*really*. Example: '[sighs] Oh, *darling*, not again.' Not: '*Sighs, leaning back.* Oh.'"
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

    unwrapped = SPAN.sub(span, text)
    spoken = TAG.sub(tag, unwrapped)
    removed = spoken != SPAN.sub(lambda match: match[1], text)
    spoken = re.sub(r"\s{2,}", " ", spoken.replace("\n", " "))
    if removed:
        spoken = REPEATED_PUNCT.sub("", SPACE_BEFORE_PUNCT.sub(r"\1", spoken))
        spoken = ORPHAN_LEAD.sub("", spoken)
    return spoken.strip()


def open_markup(text: str) -> bool:
    """An unfinished *span* or [tag], so a sentence break would cut a direction in two."""
    rest = TAG.sub("", SPAN.sub("", text))
    return "*" in rest or rest.rfind("[") > rest.rfind("]")
