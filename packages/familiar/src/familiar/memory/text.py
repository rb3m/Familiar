"""Small deterministic helpers for matching and dating memories."""

import re
import unicodedata
from datetime import datetime


def words(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(char for char in folded if not unicodedata.combining(char))
    return " ".join(re.findall(r"\w+", plain))


def date_label(timestamp: str, now: datetime) -> str:
    date = datetime.fromisoformat(timestamp).astimezone().date()
    days = (now.astimezone().date() - date).days
    relative = {0: "today", 1: "yesterday"}.get(days, f"{days} days ago")
    return f"{date.isoformat()} ({relative})"


def compact(text: str, limit: int = 39) -> str:
    tokens = text.split()
    if len(tokens) <= limit:
        return " ".join(tokens)
    return " ".join(tokens[:limit]) + "…"
