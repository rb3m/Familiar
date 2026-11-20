"""Free-text query expansion using genre and year synonyms."""

import re

from familiar.interfaces import load_data

from .index import accent_fold, tag_key

TOKEN = re.compile(r"(?:19|20)\d{2}s?|['’]?\d{2}s|[^\W_]+(?:[-/][^\W_]+)*", re.UNICODE)


def _year_range(token: str) -> tuple[int, int] | None:
    value = token.lstrip("'’")
    if re.fullmatch(r"(?:19|20)\d{2}", value):
        return int(value), int(value)
    if re.fullmatch(r"(?:19|20)\d{2}s", value):
        start = int(value[:-1])
        return start, start + 9
    if re.fullmatch(r"\d{2}s", value):
        decade = int(value[:-1])
        start = 1900 + decade if decade >= 30 else 2000 + decade
        return start, start + 9
    return None


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(TOKEN.findall(accent_fold(text)))


def _entries(terms: dict) -> dict[tuple[str, ...], dict]:
    result = {}
    for term, entry in terms.items():
        result.update({_tokens(alias): entry for alias in [term, *entry.get("aliases", [])]})
    return result


def _merge_years(current, incoming):
    if incoming is None:
        return current
    incoming = tuple(map(int, incoming))
    if current is None:
        return incoming
    intersection = max(current[0], incoming[0]), min(current[1], incoming[1])
    if intersection[0] <= intersection[1]:
        return intersection
    # Conflicting decades: the later explicit term wins.
    return incoming


def _collect_term(entry, tags, era_tags, terms):
    incoming = entry.get("tags", [])
    if entry.get("years"):
        era_tags.extend(incoming)
        return
    tags.extend(incoming)
    terms.append(frozenset(tag_key(tag) for tag in incoming))


def expand(
    text: str, vocabulary: set[str] | None = None
) -> tuple[list[str], tuple[int, int] | None, list[frozenset[str]]]:
    """Return tags, years, and distinct genre-term tag groups for relevance.

    Aliases and synonyms for one requested term count once. Eras keep broad tags
    only when no genre term matched, so a decade cannot dilute a specific genre.
    """
    data = load_data("familiar_music", "genre_synonyms").value or {}
    entries = _entries(data.get("terms", {}))
    tokens = list(_tokens(text))
    tags = []
    era_tags = []
    terms = []
    years = None
    while tokens:
        length, entry = _match_term(tokens, entries)
        if entry is not None:
            _collect_term(entry, tags, era_tags, terms)
            years = _merge_years(years, entry.get("years"))
            del tokens[:length]
            continue
        length, key = _match_vocabulary(tokens, vocabulary or set())
        if key:
            tags.append(key)
            terms.append(frozenset([key]))
            del tokens[:length]
            continue
        token = tokens.pop(0)
        years = _merge_years(years, _year_range(token))
    if not terms:
        tags.extend(era_tags)
    return list(dict.fromkeys(tags)), years, list(dict.fromkeys(terms))


def _match_term(tokens: list[str], entries: dict) -> tuple[int, dict | None]:
    for phrase in sorted(entries, key=len, reverse=True):
        if tuple(tokens[: len(phrase)]) == phrase:
            return len(phrase), entries[phrase]
    return 0, None


def _match_vocabulary(tokens: list[str], vocabulary: set[str]) -> tuple[int, str | None]:
    for length in range(len(tokens), 0, -1):
        key = tag_key(" ".join(tokens[:length]))
        if key in vocabulary:
            return length, key
    return 0, None
