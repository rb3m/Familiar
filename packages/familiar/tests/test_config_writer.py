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
    save_ptt(path, "evdev", {"device": "Thrustmaster T.Flight Hotas One", "button": "BTN_BASE3"})
    data = tomllib.loads(path.read_text())
    assert data["ptt"]["backend"] == "evdev"
    assert data["ptt"]["evdev"] == {
        "device": "Thrustmaster T.Flight Hotas One",
        "button": "BTN_BASE3",
    }


def test_mouse_round_trip_preserves_other_tables(tmp_path):
    from familiar.config import load_config

    path = tmp_path / "config.toml"
    path.write_text('# Keep me\n[llm]\nbackend = "gemini"\n\n[ptt]\nbackend = "terminal"\n')
    save_ptt(path, "mouse", {"button": 4, "consume": False})
    assert path.read_text().startswith('# Keep me\n[llm]\nbackend = "gemini"')
    config = load_config(path)
    assert config.ptt.backend == "mouse"
    assert config.ptt.settings == {"button": 4, "consume": False}
    save_ptt(path, "evdev", {"device": "Stick", "button": "BTN_TRIGGER"})
    assert "[ptt.mouse]" not in path.read_text()
    assert load_config(path).ptt.settings == {"device": "Stick", "button": "BTN_TRIGGER"}


def test_save_ptt_twice_replaces_section_with_bare_keys(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('language = "en"\n\n[ptt]\nbackend = "terminal"\n\n[music]\nenabled = true\n')
    save_ptt(path, "mouse", {"button": 4})
    save_ptt(path, "mouse", {"button": 3})
    text = path.read_text()
    assert text.count("[ptt]") == 1
    assert "button = 3" in text
    assert tomllib.loads(text)["music"] == {"enabled": True}
