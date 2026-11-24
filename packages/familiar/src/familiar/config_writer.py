"""Small, comment-preserving edits to config.toml (only the tables we own are rewritten)."""

import json
import re
from pathlib import Path

_TABLE = re.compile(r"^\s*#?\s*\[\[?([A-Za-z0-9_.\-]+)\]\]?\s*(?:#.*)?$")


def _table_name(line: str) -> str | None:
    match = _TABLE.match(line)
    return match.group(1) if match else None


def _is_section(name: str | None, section: str) -> bool:
    return name is not None and (name == section or name.startswith(f"{section}."))


def replace_section(text: str, section: str, block: str) -> str:
    """Replace section tables and array tables (including commented headers) with block.

    The block is appended when the section does not exist yet. Other tables and comments are
    left untouched.
    """
    kept: list[str] = []
    inside = False
    inserted = False
    for line in text.splitlines():
        name = _table_name(line)
        if name is not None:
            inside = _is_section(name, section)
        if inside and not inserted:
            kept.append(block.rstrip())
            kept.append("")
            inserted = True
        if inside:
            continue
        kept.append(line)
    if not inserted:
        kept.extend(["", block.rstrip()])
    return "\n".join(kept).rstrip() + "\n"


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(key: str) -> str:
    return key if _BARE_KEY.match(key) else json.dumps(key)


def _value(value: object) -> str:
    if isinstance(value, dict):
        entries = (f"{_key(key)} = {_value(item)}" for key, item in value.items())
        return "{" + ", ".join(entries) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False)


def ptt_block(bindings: list[dict]) -> str:
    lines = ["[ptt]"]
    for binding in bindings:
        lines.extend(["", "[[ptt.bindings]]", f"backend = {json.dumps(binding['backend'])}"])
        lines.extend(f"{_key(key)} = {_value(value)}" for key, value in binding["settings"].items())
    return "\n".join(lines) + "\n"


def save_ptt(path: Path, bindings: list[dict]) -> None:
    """Save push-to-talk bindings, creating the file if needed."""
    text = path.read_text() if path.is_file() else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(replace_section(text, "ptt", ptt_block(bindings)))
