"""Small shared persona fixture, independent of repository persona files."""

import pytest
from familiar.persona import Persona


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
