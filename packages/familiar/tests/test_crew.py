"""Exact addressing and backend-independent event/idle arbitration."""

import random
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from familiar.crew import Crew, interest
from familiar.director import Director
from familiar.interfaces import GameEvent


def event(kind="exploration_hint", priority="notable"):
    return GameEvent(kind, priority, {"summary": "A grounded event."}, datetime.now(UTC))


@pytest.mark.parametrize(
    "text,language,expected",
    [
        ("FRIEREN, what is that?", "en", "frieren"),
        ("Hola, Mirita!", "es", "mira"),
        ("Friren, ¿qué ves?", "es", "frieren"),
        ("フリーレン、見て。", "ja", "frieren"),
        ("ミラ！", "ja", "mira"),
        ("Megumin, ask Mira", "en", "megumin"),
        ("Mira and Frieren", "en", "mira"),
        ("Frierenish", "en", "mira"),
        ("premira", "en", "mira"),
        ("Fryren, hello", "en", "mira"),
        ("Friren", "en", "mira"),
    ],
)
def test_aliases_first_position_word_boundaries(crew, text, language, expected):
    assert crew.pick_for_utterance(text, language).id == expected
    assert crew.last_speaker is None  # selection is pure


def test_followup_only_after_heard_and_expires(crew):
    frieren = crew.members[1]
    crew.note_spoke(frieren)
    assert crew.pick_for_utterance("And then?", "en") == frieren
    assert crew.pick_for_utterance("Mira?", "en") == crew.carrier
    crew.clock = lambda: 100 + crew.follow_up_window_s + 0.01
    assert crew.pick_for_utterance("And then?", "en") == crew.carrier


def test_exact_longest_prefix_and_chattiness(crew):
    frieren = crew.members[1]
    assert interest(frieren, "exploration_hint") == 3
    assert interest(frieren, "exploration_hint_more") == 3
    assert interest(frieren, "exploration_other") == 2
    assert crew.pick_for_event(event()).id == "frieren"
    assert crew.pick_for_event(event("combat_danger")).id == "megumin"
    assert crew.pick_for_event(event(priority="critical")) == crew.carrier
    quiet = replace(frieren, chattiness=0.01)
    assert Crew([crew.carrier, quiet], "mira").pick_for_event(event()) == crew.carrier


def test_ties_carrier_then_roster(crew):
    assert crew.pick_for_event(event("unknown")) == crew.carrier
    members = [replace(member, interests={"test": 2}) for member in crew.members]
    members[0] = replace(members[0], interests={"test": 0})
    assert Crew(members, "mira").pick_for_event(event("test")) == members[1]


def test_idle_weights_no_consecutive_repeat_and_zero_fallback(crew):
    rng = random.Random(42)
    counts = dict.fromkeys([member.id for member in crew.members], 0)
    for _ in range(500):
        member = crew.pick_idle(rng)
        assert member != crew.last_speaker
        counts[member.id] += 1
        crew.note_spoke(member)
    assert all(count > 100 for count in counts.values())
    single = Crew([crew.carrier], "mira")
    single.note_spoke(single.carrier)
    assert single.pick_idle(rng) == single.carrier
    zeros = Crew([replace(member, chattiness=0) for member in crew.members], "mira")
    assert zeros.pick_idle(rng) == zeros.members[0]
    weighted = Crew([replace(crew.members[0], chattiness=0), crew.members[1]], "mira")
    assert weighted.pick_idle(rng) == crew.members[1]


@pytest.mark.parametrize(
    "ids,carrier", [([], "mira"), ([0, 0], "mira"), ([0, 1], "outsider"), ([0, 1, 2, 0], "mira")]
)
def test_invalid_roster(crew, ids, carrier):
    with pytest.raises(ValueError):
        Crew([crew.members[index] for index in ids], carrier)


def test_director_assigns_and_critical_preempts(crew):
    director = Director(0.5, {}, crew=crew, rng=random.Random(0))
    director.set_game_state(True, False)
    director.submit(event())
    assert director._take().data["speaker_id"] == "frieren"
    director.set_busy("speaking", True)
    director.submit(event("danger", "critical"))
    assert director._take().data["speaker_id"] == "mira"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Frieren: Hello.", "Hello."),
        ("  MIRITA : Hola.", "Hola."),
        ("フリーレン：はい。", "はい。"),
        ("Miracle: Hello.", "Miracle: Hello."),
    ],
)
def test_strip_any_crew_alias(crew, text, expected):
    assert crew.strip_prefix(text, "es" if "MIRITA" in text else "ja") == expected


def test_prefix_accepts_alias_from_another_language(crew):
    assert crew.strip_prefix("フリーレン: Hello.", "en") == "Hello."


def test_director_idle_avoids_last_heard_member(crew):
    crew.note_spoke(crew.members[1])
    director = Director(0.5, {}, crew=crew, rng=random.Random(2))
    assigned = director._assign(event("idle", "ambient"))
    assert assigned.data["speaker_id"] != "frieren"
