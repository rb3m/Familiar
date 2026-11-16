"""Small, comment-preserving edits to config.toml (only the tables we own are rewritten)."""

import json
import re
from pathlib import Path

_TABLE = re.compile(r"^\s*#?\s*\[([A-Za-z0-9_.\-]+)\]\s*$")


def _table_name(line: str) -> str | None:
    match = _TABLE.match(line)
    return match.group(1) if match else None


def _is_section(name: str | None, section: str) -> bool:
    return name is not None and (name == section or name.startswith(f"{section}."))


def replace_section(text: str, section: str, block: str) -> str:
    """Replace `[section]` and its `[section.*]` sub-tables (commented ones too) with block.

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


def ptt_block(device: str, button: str) -> str:
    return (
        "[ptt]\n"
        'backend = "evdev"          # "terminal" = Enter to start/stop\n'
        "\n"
        "[ptt.evdev]                 # written by `familiar ptt-setup`\n"
        f"device = {json.dumps(device)}\n"
        f"button = {json.dumps(button)}\n"
    )


def save_ptt(path: Path, device: str, button: str) -> None:
    """Point the config at an evdev push-to-talk button, creating the file if needed."""
    text = path.read_text() if path.is_file() else ""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(replace_section(text, "ptt", ptt_block(device, button)))
