"""Persona validation and prompt composition."""

from dataclasses import asdict

import pytest
import yaml

from familiar.interfaces import ConfigError
from familiar.persona import load_persona


def test_load_prompt_voice(tmp_path, persona):
    (tmp_path / "test.yaml").write_text(yaml.safe_dump(asdict(persona)))
    loaded = load_persona(tmp_path, "test")
    assert loaded == persona
    prompt = loaded.system_prompt("en", "ats")
    assert "Be thoughtful." in prompt
    assert "- Be brief." in prompt
    assert "Notice the road." in prompt
    assert "Always reply in English." in prompt
    assert loaded.voice("gemini", "en") == "Kore"
    assert loaded.voice("other", "en") is None
    assert loaded.voice("missing", "ja") is None
    with pytest.raises(ConfigError, match="does not support"):
        loaded.system_prompt("ja")


@pytest.mark.parametrize("value", [-0.1, 1.1, "0.5", None, True, float("nan")])
def test_bad_chattiness(tmp_path, persona, value):
    (tmp_path / "test.yaml").write_text(yaml.safe_dump({**asdict(persona), "chattiness": value}))
    with pytest.raises(ConfigError, match="chattiness"):
        load_persona(tmp_path, "test")


@pytest.mark.parametrize(
    "change",
    [{"languages": {}}, {"languages": []}, {"prompt": 5}, {"style_rules": [1]}, {"game_hooks": []}],
)
def test_invalid_schema(tmp_path, persona, change):
    (tmp_path / "test.yaml").write_text(yaml.safe_dump({**asdict(persona), **change}))
    with pytest.raises(ConfigError):
        load_persona(tmp_path, "test")


def test_missing_persona(tmp_path):
    with pytest.raises(ConfigError):
        load_persona(tmp_path, "missing")
