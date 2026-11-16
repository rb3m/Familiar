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
    save_ptt(path, "Thrustmaster T.Flight Hotas One", "BTN_BASE3")
    data = tomllib.loads(path.read_text())
    assert data["ptt"]["backend"] == "evdev"
    assert data["ptt"]["evdev"] == {
        "device": "Thrustmaster T.Flight Hotas One",
        "button": "BTN_BASE3",
    }
