from familiar import intents
from familiar.intents import action_roles


def test_music_phrases_in_english_and_spanish():
    assert action_roles("Ruslana, play some funky music", "en") == {"dj"}
    assert action_roles("Skip this one", "en") == {"dj"}
    assert action_roles("Ruslana, pon algo de rock", "es") == {"dj"}
    assert action_roles("Toca Viejo Veneno de La Castañeda", "es") == {"dj"}


def test_whole_words_only_and_chat_stays_free():
    assert action_roles("What's playing? Nice playlist", "en") == frozenset()
    assert action_roles("How are you all doing?", "en") == frozenset()
    assert action_roles("¿Qué opinas de esta canción?", "es") == frozenset()
    assert action_roles("Play some music", "xx") == frozenset()


def test_a_role_with_every_phrase_off_matches_nothing(monkeypatch):
    class Data:
        value = {"dj": {"en": {"play": False}}}

    intents._patterns.cache_clear()
    monkeypatch.setattr(intents, "load_data", lambda *_: Data)
    try:
        assert action_roles("anything at all", "en") == frozenset()
    finally:
        intents._patterns.cache_clear()
