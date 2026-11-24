"""config_writer rewrites only the [ptt] tables and keeps everything else."""

import tomllib

from familiar.config_writer import replace_section, save_ptt

ORIGINAL = """language = "en"   # keep me

[tts]
backend = "elevenlabs"

[ptt]
backend = "terminal"       # Enter starts recording

# [ptt.evdev]
# device = "Thrustmaster"

[stt]
backend = "gemini"
"""


def test_replaces_ptt_and_commented_subtables_only():
    out = replace_section(ORIGINAL, "ptt", '[ptt]\nbackend = "evdev"\n')
    data = tomllib.loads(out)
    assert data["ptt"] == {"backend": "evdev"}
    assert data["tts"]["backend"] == "elevenlabs"
    assert data["stt"]["backend"] == "gemini"
    assert "# keep me" in out
    assert "Thrustmaster" not in out


def test_appends_when_missing():
    out = replace_section('language = "en"\n', "ptt", '[ptt]\nbackend = "evdev"\n')
    assert tomllib.loads(out)["ptt"]["backend"] == "evdev"


def test_save_ptt_creates_file(tmp_path):
    path = tmp_path / "sub" / "config.toml"
    save_ptt(
        path,
        [
            {
                "backend": "evdev",
                "settings": {"device": "Thrustmaster T.Flight Hotas One", "button": "BTN_BASE3"},
            }
        ],
    )
    data = tomllib.loads(path.read_text())
    assert data["ptt"]["bindings"][0]["backend"] == "evdev"
    assert data["ptt"]["bindings"][0] == {
        "backend": "evdev",
        "device": "Thrustmaster T.Flight Hotas One",
        "button": "BTN_BASE3",
    }


def test_mouse_round_trip_preserves_other_tables(tmp_path):
    from familiar.config import load_config

    path = tmp_path / "config.toml"
    path.write_text('# Keep me\n[llm]\nbackend = "gemini"\n\n[ptt]\nbackend = "terminal"\n')
    save_ptt(path, [{"backend": "mouse", "settings": {"button": 4, "consume": False}}])
    assert path.read_text().startswith('# Keep me\n[llm]\nbackend = "gemini"')
    config = load_config(path)
    assert config.ptt[0].backend == "mouse"
    assert config.ptt[0].settings == {"button": 4, "consume": False}
    save_ptt(path, [{"backend": "evdev", "settings": {"device": "Stick", "button": "BTN_TRIGGER"}}])
    assert "[ptt.mouse]" not in path.read_text()
    assert load_config(path).ptt[0].settings == {"device": "Stick", "button": "BTN_TRIGGER"}


def test_save_ptt_twice_replaces_section_with_bare_keys(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('language = "en"\n\n[ptt]\nbackend = "terminal"\n\n[music]\nenabled = true\n')
    save_ptt(path, [{"backend": "mouse", "settings": {"button": 4}}])
    save_ptt(path, [{"backend": "mouse", "settings": {"button": 3}}])
    text = path.read_text()
    assert text.count("[ptt]") == 1
    assert "button = 3" in text
    assert tomllib.loads(text)["music"] == {"enabled": True}


def test_multiple_bindings_round_trip_removes_stale_tables(tmp_path):
    from familiar.config import load_config

    path = tmp_path / "config.toml"
    path.write_text(ORIGINAL)
    bindings = [
        {"backend": "evdev", "settings": {"device": "Keychron", "button": "KEY_F13"}},
        {"backend": "mouse", "settings": {"button": 4}},
    ]
    save_ptt(path, bindings)
    save_ptt(path, bindings)
    assert len(load_config(path).ptt) == 2
    assert path.read_text().count("[[ptt.bindings]]") == 2
    save_ptt(path, bindings[1:])
    assert len(load_config(path).ptt) == 1
    assert "Keychron" not in path.read_text()
    assert tomllib.loads(path.read_text())["stt"]["backend"] == "gemini"


def test_config_binding_list_rules(tmp_path):
    import pytest
    from familiar.config import load_config
    from familiar.interfaces import ConfigError

    path = tmp_path / "config.toml"
    invalid = [
        "[ptt]\nbindings = []",
        '[ptt]\nbackend = "terminal"\n[[ptt.bindings]]\nbackend = "mouse"\nbutton = 4',
        '[ptt.mouse]\nbutton = 4\n[[ptt.bindings]]\nbackend = "mouse"\nbutton = 4',
        '[[ptt.bindings]]\nbackend = "terminal"\n[[ptt.bindings]]\nbackend = "mouse"\nbutton = 4',
        (
            '[[ptt.bindings]]\nbackend = "mouse"\nbutton = 4\n'
            '[[ptt.bindings]]\nbackend = "mouse"\nbutton = 4\nconsume = true'
        ),
        (
            '[[ptt.bindings]]\nbackend = "key"\nkey = 105\nmodifiers = ["shift", "control"]\n'
            '[[ptt.bindings]]\nbackend = "key"\nkey = 105\nmodifiers = ["control", "shift"]'
        ),
    ]
    for text in invalid:
        path.write_text(text)
        with pytest.raises(ConfigError):
            load_config(path)
    path.write_text('[ptt]\nbackend = "mouse"\n[ptt.mouse]\nbutton = 4')
    assert load_config(path).ptt[0].settings == {"button": 4}
    path.write_text("")
    assert load_config(path).ptt[0].backend == "terminal"


def test_custom_binding_settings_round_trip(tmp_path):
    from familiar.config import load_config

    path = tmp_path / "config.toml"
    settings = {"nested": {"a key": "value"}, "items": [1, "two"], "enabled": True}
    save_ptt(path, [{"backend": "custom", "settings": settings}])
    assert load_config(path).ptt[0].settings == settings


def test_replace_removes_scattered_and_commented_binding_tables():
    text = (
        '[ptt]\nbackend = "terminal"\n'
        "[music]\nenabled = true\n"
        '[[ptt.bindings]]\nbackend = "mouse"\nbutton = 4\n'
        '# [[ptt.bindings]]\n# backend = "old"\n'
        '[stt]\nbackend = "gemini"\n'
    )
    out = replace_section(text, "ptt", '[ptt]\n[[ptt.bindings]]\nbackend = "terminal"\n')
    assert tomllib.loads(out)["ptt"] == {"bindings": [{"backend": "terminal"}]}
    assert tomllib.loads(out)["music"] == {"enabled": True}
    assert tomllib.loads(out)["stt"] == {"backend": "gemini"}
    assert out.count("[[ptt.bindings]]") == 1
