"""Atomic persistence for user personas."""

import os
import tempfile
from pathlib import Path

import yaml

from familiar.persona import validate_persona


def save_persona(directory: Path, data: dict) -> dict:
    """Validate before replacing a persona, including on concurrent reads."""
    data = validate_persona(data)
    directory.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, suffix=".tmp", delete=False
        ) as handle:
            name = handle.name
            yaml.safe_dump(data, handle, sort_keys=False, allow_unicode=True)
        os.replace(name, directory / f"{data['id']}.yaml")
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)
    return data
