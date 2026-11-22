"""Mask booking identifiers before exposing lookup text."""

import re
from functools import cache

from familiar.interfaces.data import load_data


class Masker:
    def __init__(self, settings: dict) -> None:
        self.patterns = tuple(re.compile(pattern) for pattern in settings["mask_patterns"])
        self.replacement = settings["mask_with"]

    def mask(self, text: str) -> str:
        for pattern in self.patterns:
            text = pattern.sub(self.replacement, text)
        return text


@cache
def _default_masker() -> Masker:
    return Masker(load_data("familiar_research", "agy").value)


def mask(text: str) -> str:
    """Mask text using the loaded default profile, compiled on first use."""
    return _default_masker().mask(text)
