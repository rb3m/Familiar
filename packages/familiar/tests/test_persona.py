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


def _write(tmp_path, extra=""):
    (tmp_path / "p.yaml").write_text(
        "id: p\ndisplay_name: P\nsource: S\nchattiness: 0.5\nprompt: Hi\n"
        "languages: {en: {voices: {}}}\n" + extra
    )


def test_truthfulness_defaults_to_strict_and_keeps_practical_facts(tmp_path):
    from familiar.persona import (
        PRACTICAL_FACTS_RULE,
        SPOKEN_RULE,
        TRUTHFULNESS_RULES,
        load_persona,
    )

    _write(tmp_path)
    prompt = load_persona(tmp_path, "p").system_prompt("en")
    assert TRUTHFULNESS_RULES["strict"] in prompt
    assert PRACTICAL_FACTS_RULE in prompt
    assert SPOKEN_RULE in prompt


def test_playful_persona_may_exaggerate_but_practical_facts_stay(tmp_path):
    from familiar.persona import PRACTICAL_FACTS_RULE, TRUTHFULNESS_RULES, load_persona

    _write(tmp_path, "truthfulness: playful\n")
    prompt = load_persona(tmp_path, "p").system_prompt("en")
    assert TRUTHFULNESS_RULES["playful"] in prompt
    assert TRUTHFULNESS_RULES["strict"] not in prompt
    assert PRACTICAL_FACTS_RULE in prompt


def test_unknown_truthfulness_rejected(tmp_path):
    import pytest
    from familiar.interfaces import ConfigError
    from familiar.persona import load_persona

    _write(tmp_path, "truthfulness: chaotic\n")
    with pytest.raises(ConfigError, match="truthfulness"):
        load_persona(tmp_path, "p")


def test_packaged_persona_and_search_precedence(tmp_path, persona):
    user = tmp_path / "user"
    configured = tmp_path / "configured"
    builtin = load_persona(configured, "mira", user)
    assert builtin.display_name == "Mira"
    assert builtin.truthfulness == "strict"
    assert builtin.chattiness == 0.5
    assert builtin.voice("gemini", "en") == "Kore"
    configured.mkdir()
    data = {**asdict(persona), "id": "mira", "display_name": "Configured"}
    (configured / "mira.yaml").write_text(yaml.safe_dump(data))
    assert load_persona(configured, "mira", user).display_name == "Configured"
    user.mkdir()
    (user / "mira.yaml").write_text(yaml.safe_dump({**data, "display_name": "User"}))
    assert load_persona(configured, "mira", user).display_name == "User"
