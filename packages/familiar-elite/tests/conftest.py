"""Hermetic local game files; never use a player's data overrides."""

import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_data(monkeypatch, tmp_path):
    monkeypatch.setenv("FAMILIAR_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def journal_dir(tmp_path):
    fixture = Path(__file__).parent / "fixtures/Journal.20261117120000.01.log"
    (tmp_path / fixture.name).write_bytes(fixture.read_bytes())
    return tmp_path


@pytest.fixture
def write_snapshot():
    def write(directory, name="Status", **values):
        data = {"timestamp": "2026-11-17T12:01:00Z", "event": name, **values}
        (directory / f"{name}.json").write_text(json.dumps(data))

    return write
