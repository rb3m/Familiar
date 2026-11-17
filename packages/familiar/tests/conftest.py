"""Small shared persona fixture, independent of repository persona files."""

import pytest
from familiar.persona import Persona


@pytest.fixture(autouse=True)
def isolated_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("FAMILIAR_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def persona():
    return Persona(
        id="test",
        display_name="Test",
        source="Test source",
        chattiness=0.3,
        prompt="Be thoughtful.",
        style_rules=["Be brief."],
        languages={"en": {"voices": {"gemini": "Kore", "other": None}}},
        game_hooks={"ats": "Notice the road."},
    )


@pytest.fixture
def store(tmp_path):
    from familiar.memory import MemoryStore

    value = MemoryStore(tmp_path / "memory.db")
    yield value
    value.close()
