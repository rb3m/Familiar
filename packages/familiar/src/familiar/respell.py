"""Persona respellings: swap written words for forms a voice pronounces correctly.

A persona may write foreign words in Latin letters ("Bože moj", "davai"). Voices read those with
English sounds, so `languages.<code>.spoken` maps them to the native script ("Боже мой"). Only
the spoken text changes; transcripts keep what was written. Matching ignores case and
diacritics, works on whole words, and prefers the longest phrase. A key wrapped in asterisks
("*net*": "*нет*") matches only the emphasised word, for words that are also English.
"""

import re
import unicodedata
from functools import lru_cache


def fold(text: str) -> str:
    """Lowercase and drop diacritics, keeping one output character per input character."""
    return "".join(_fold_char(char) for char in text)


def _fold_char(char: str) -> str:
    base = unicodedata.normalize("NFKD", char)[:1].casefold()
    return base if len(base) == 1 else char


@lru_cache(maxsize=32)
def _compile(keys: tuple[str, ...]) -> re.Pattern[str]:
    # Longest first, so "bože moj" wins over "bože" and "nu davai" over "davai".
    phrases = sorted((re.escape(fold(key)) for key in keys), key=len, reverse=True)
    return re.compile(rf"(?<!\w)(?:{'|'.join(phrases)})(?!\w)")


def respell(text: str, table: dict[str, str]) -> str:
    """Replace every table phrase found in `text`; keys are matched case/diacritic-blind."""
    if not table or not text:
        return text
    targets = {fold(key): value for key, value in table.items()}
    pattern = _compile(tuple(sorted(table)))
    folded = fold(text)
    parts = []
    position = 0
    for match in pattern.finditer(folded):
        parts.append(text[position : match.start()])
        parts.append(_match_case(text[match.start() : match.end()], targets[match.group()]))
        position = match.end()
    parts.append(text[position:])
    return "".join(parts)


def _match_case(written: str, spoken: str) -> str:
    """Capitalise the replacement's first letter when the written word's first letter was."""
    first = next((char for char in written if char.isalpha()), "")
    index = next((i for i, char in enumerate(spoken) if char.isalpha()), None)
    if not first.isupper() or index is None:
        return spoken
    return spoken[:index] + spoken[index].upper() + spoken[index + 1 :]
