"""Validated, ungated quote candidates supplied to the core."""

import re
from importlib.resources import files
from pathlib import Path

import yaml

from .events import EVENT_KINDS

FIELDS = {"id", "text", "speaker", "context", "attribution", "triggers", "source_url"}


def load_quotes(path: str | Path | None = None) -> list[dict]:
    resource = (
        Path(path) if path is not None else files("familiar_ksp").joinpath("knowledge/quotes.yaml")
    )
    entries = yaml.safe_load(resource.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise ValueError("Quotes must be a list")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != FIELDS:
            raise ValueError("Quote fields must be: " + ", ".join(sorted(FIELDS)))
        for field in FIELDS - {"triggers"}:
            if not isinstance(entry[field], str) or not entry[field].strip():
                raise ValueError(f"Quote {field} must be a nonempty string")
        slug = entry["id"]
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or slug in seen:
            raise ValueError(f"Invalid or duplicate quote id: {slug}")
        seen.add(slug)
        if entry["attribution"] not in {"verified", "attributed"}:
            raise ValueError("Invalid quote attribution")
        triggers = entry["triggers"]
        if (
            not isinstance(triggers, list)
            or not triggers
            or any(not isinstance(kind, str) or kind not in EVENT_KINDS for kind in triggers)
        ):
            raise ValueError("Invalid quote triggers")
        if not entry["source_url"].startswith(("https://", "http://")):
            raise ValueError("Quote source_url must be an HTTP URL")
    return entries


def attach_flavor(event, quotes: list[dict]) -> None:
    candidates = [
        {key: entry[key] for key in ("id", "text", "speaker", "context", "attribution")}
        for entry in quotes
        if event.kind in entry["triggers"]
    ]
    if candidates:
        event.data["flavor"] = candidates
