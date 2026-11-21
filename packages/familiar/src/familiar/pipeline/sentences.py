"""Incrementally split text without breaking decimals or common abbreviations."""

import re

from familiar.speech import open_markup

_ABBREVIATION = re.compile(r"\b(?:mr|mrs|ms|dr|prof|sr|jr|e\.g|i\.e|vs|etc)\.$", re.I)
_BOUNDARY = re.compile(r"\n|[。！？]+|[.!?…]+(?=\s)")


class SentenceSplitter:
    """Buffer deltas until a sentence boundary or a maximum-length split."""

    def __init__(self, max_chars: int = 240) -> None:
        if max_chars < 1:
            raise ValueError("max_chars must be positive")
        self.max_chars = max_chars
        self._buffer = ""

    def _boundary(self) -> int | None:
        for match in _BOUNDARY.finditer(self._buffer):
            end = match.end()
            if match.group() == "." and _ABBREVIATION.search(self._buffer[:end]):
                continue
            if open_markup(self._buffer[:end]):
                continue  # don't cut "*She sighs. Leans back.*" in two
            return end
        return None

    def _forced_cut(self) -> int | None:
        """Cut an over-long buffer at the last space, or hard at max_chars."""
        if len(self._buffer) <= self.max_chars:
            return None
        space = self._buffer.rfind(" ", 0, self.max_chars + 1)
        return space if space > 0 else self.max_chars

    def _next_cut(self) -> int | None:
        end = self._boundary()
        if end is not None and end <= self.max_chars:
            return end
        return self._forced_cut()

    def feed(self, delta: str) -> list[str]:
        """Return complete sentences; Latin punctuation awaits whitespace."""
        self._buffer += delta
        sentences = []
        while (end := self._next_cut()) is not None:
            sentence = self._buffer[:end].strip()
            self._buffer = self._buffer[end:].lstrip()
            if sentence:
                sentences.append(sentence)
        return sentences

    def flush(self) -> str | None:
        """Return the remaining text once, including an unfinished sentence."""
        remainder = self._buffer.strip()
        self._buffer = ""
        return remainder or None
