"""Persona respellings: Latin-script foreign words become forms the voice reads correctly."""

import pytest
from familiar.interfaces import ConfigError
from familiar.persona import validate_persona
from familiar.respell import fold, respell

TABLE = {
    "bože moj": "боже мой",
    "bože": "боже",
    "davai": "давай",
    "nu davai": "ну давай",
    "da": "да",
    "*net*": "*нет*",
}


def test_fold_keeps_length_and_drops_diacritics():
    assert fold("Bože Otlično") == "boze otlicno"
    assert len(fold("Bože Otlično")) == len("Bože Otlično")


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("Bože moj, how boring!", "Боже мой, how boring!"),
        ("*bozhe*", "*bozhe*"),  # not a table entry
        ("BOZE MOJ...", "Боже мой..."),  # diacritic- and case-blind, capitalised
        ("Nu davai, play it!", "Ну давай, play it!"),  # longest phrase wins
        ("Davai! Da!", "Давай! Да!"),
        ("Dad and daisies", "Dad and daisies"),  # whole words only
        ("*Net*, no music.", "*Нет*, no music."),  # emphasis-only key
        ("The safety net is gone.", "The safety net is gone."),
        ("", ""),
    ],
)
def test_respell(written, spoken):
    assert respell(written, TABLE) == spoken


def test_respell_without_table_is_identity():
    assert respell("Bože moj", {}) == "Bože moj"


def persona(spoken):
    return {
        "id": "ruslana",
        "display_name": "Ruslana",
        "source": "tests",
        "chattiness": 0.5,
        "prompt": "DJ.",
        "languages": {"en": {"voices": {}, "spoken": spoken}},
    }


def test_persona_spoken_validated():
    assert validate_persona(persona({"da": "да"}))["languages"]["en"]["spoken"] == {"da": "да"}
    for bad in ({"da": ""}, {"": "да"}, ["da"], {"da": 1}):
        with pytest.raises(ConfigError, match="spoken"):
            validate_persona(persona(bad))
