"""Deterministic recall gates and local, dated player-turn context."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from familiar.interfaces import GameEvent
from familiar.memory import MemoryKeeper
from familiar.memory.keeper import MEMORY_HEADER, RECALL_EPISODE_COOLDOWN
from familiar.memory.text import date_label
from test_memory_store import NOW, add

LOCATION = dict(
    place_key="FR/Château-Thierry",
    place="Château-Thierry",
    region="France",
    activity="hauling 18 tons of cattle to Bayonne",
)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def event(kind="traffic_jam", salience=0.5, arrival=False):
    return GameEvent(
        kind,
        "notable",
        {
            "summary": "Traffic jam on the motorway.",
            "memory_salience": salience,
            "memory_arrival": arrival,
        },
        NOW,
    )


def keeper(store, location=LOCATION, probability=1, persona="mira"):
    clock = Clock()
    result = MemoryKeeper(
        store,
        "ets2",
        persona,
        lambda: location,
        clock,
        SimpleNamespace(random=lambda: 0.2),
        {"recall_probability": probability},
    )
    return result, clock


def earlier(store, **fields):
    session = store.start_session("ets2", "other", NOW - timedelta(days=1))
    identifier = add(store, session, now=NOW - timedelta(days=1), **fields)
    return session, identifier


def test_observe_salience_location_and_duplicate_collapse(store):
    location = LOCATION.copy()
    memory, _ = keeper(store, location)
    memory.observe(event(salience=0))
    assert store.revision == 0
    memory.observe(event())
    memory.observe(event())
    assert store.revision == 1
    row = dict(store.db.execute("SELECT * FROM episodes").fetchone())
    assert row["place_key"] == LOCATION["place_key"]
    assert row["activity"] == LOCATION["activity"]
    assert row["text"] == "Traffic jam on the motorway."
    location.update(place_key="FR/Paris", place="Paris")
    memory.observe(event())
    memory.observe(event(kind="collision", salience=0.8))
    assert store.revision == 3
    absent, _ = keeper(store, None)
    absent.observe(event())
    MemoryKeeper(store, "", "mira").observe(event())
    assert store.revision == 3


def test_recall_earlier_session_only_and_salience(store):
    earlier(store, kind="passing_town", salience=0.1)
    memory, _ = keeper(store)
    memory.observe(event(salience=0.8))
    later = store.start_session("ets2", "mira", NOW)
    add(store, later, kind="fined", salience=1)
    assert memory.maybe_recall(event(arrival=True)) is None
    assert memory.maybe_recall(event()) is None
    assert keeper(store, None)[0].maybe_recall(event(arrival=True)) is None


def test_recall_most_salient_and_not_recorded_until_spoken(store):
    earlier(store)
    _, identifier = earlier(store, kind="collision", salience=0.8)
    memory, _ = keeper(store)
    recall = memory.maybe_recall(event(arrival=True))
    assert recall.kind == "memory_recall" and recall.priority == "ambient"
    assert recall.data["ttl_s"] == 120
    assert recall.data["memory_episode_id"] == identifier
    assert (
        f"Memory from {date_label((NOW - timedelta(days=1)).isoformat(), NOW)}"
        in recall.data["summary"]
    )
    assert "not happening now: near Château-Thierry, France" in recall.data["summary"]
    assert "hauling 18 tons of cattle to Bayonne" in recall.data["summary"]
    assert store.latest_recall() is None
    memory.recalled(recall)
    assert store.last_recall(identifier, "mira") == NOW


@pytest.mark.parametrize(
    "probability,value,allowed",
    [(0, 0, False), (0.25, 0.25, False), (0.25, 0.24, True), (1, 0.99, True)],
)
def test_probability_gate(store, probability, value, allowed):
    earlier(store)
    memory, _ = keeper(store, probability=probability)
    memory.rng = SimpleNamespace(random=lambda: value)
    assert (memory.maybe_recall(event(arrival=True)) is not None) is allowed


def test_global_and_per_episode_cooldowns_survive_sessions(store):
    _, identifier = earlier(store)
    memory, clock = keeper(store)
    recall = memory.maybe_recall(event(arrival=True))
    memory.recalled(recall)
    clock.now += timedelta(minutes=45)
    assert memory.maybe_recall(event(arrival=True)) is None  # seven-day episode gate
    other, other_clock = keeper(store, persona="other")
    other_clock.now += timedelta(minutes=44, seconds=59)
    assert other.maybe_recall(event(arrival=True)) is None
    other_clock.now += timedelta(seconds=1)
    assert other.maybe_recall(event(arrival=True)) is not None
    clock.now = NOW + RECALL_EPISODE_COOLDOWN - timedelta(seconds=1)
    assert memory.maybe_recall(event(arrival=True)) is None
    clock.now += timedelta(seconds=1)
    assert memory.maybe_recall(event(arrival=True)).data["memory_episode_id"] == identifier


def test_global_cooldown_applies_between_episodes_and_configurable(store):
    _, identifier = earlier(store, place_key="FR/Elsewhere", place="Elsewhere")
    earlier(store)
    store.record_recall(identifier, "someone", NOW)
    memory, clock = keeper(store)
    assert memory.maybe_recall(event(arrival=True)) is None
    clock.now += timedelta(minutes=45)
    assert memory.maybe_recall(event(arrival=True)) is not None
    memory.cooldown = timedelta(minutes=60)
    assert memory.maybe_recall(event(arrival=True)) is None


def test_context_local_date_and_no_current_session_leak(store):
    earlier(store, text="Old traffic jam.")
    memory, _ = keeper(store)
    memory.observe(event(kind="fined", salience=0.9))
    text = memory.context("hello")
    assert text.startswith(MEMORY_HEADER)
    assert "Old traffic jam." in text
    assert "yesterday" in text
    assert "Traffic jam on the motorway." not in text
    assert len(text.splitlines()[1].split()) <= 40


def test_context_region_fallback_and_no_record(store):
    earlier(store)
    memory, _ = keeper(store, LOCATION | {"place_key": "FR/Paris", "place": "Paris"})
    text = memory.context("Hello")
    assert "First recorded time near Paris; we were in France on" in text
    assert "Château-Thierry" in text
    memory.whereabouts = lambda: (
        LOCATION | {"place_key": "DE/Berlin", "place": "Berlin", "region": "Germany"}
    )
    assert "No record of earlier visits near Berlin." in memory.context("hello")


@pytest.mark.parametrize("query", ["CHATEAU-THIERRY?", "France", "CATTLE", "Bayonne!"])
def test_context_term_matches_accents_case_and_whole_words(store, query):
    earlier(store)
    memory, _ = keeper(store, None)
    assert "Traffic jam" in memory.context(query)
    assert memory.context("chateau-thierryish catt bayonnes") == ""
    assert memory.context("hello") == ""


def test_term_cache_refreshes_after_added_episodes(store):
    memory, _ = keeper(store, None)
    assert memory.context("Reno") == ""
    # An episode added in an earlier session makes the previously unknown term available.
    old = store.start_session("ets2", "other", NOW)
    memory.session_id = store.start_session("ets2", "mira", NOW)
    add(store, old, place="Reno", place_key="NV/Reno", region="Nevada")
    assert "near Reno" in memory.context("RÉNO")
    memory.observe(event())
    assert "reno" in memory._terms


def test_context_caps_episodes_and_includes_named_match(store):
    earlier(store, kind="collision", salience=0.8, text="First local fact.")
    earlier(store, kind="fined", salience=0.7, text="Second local fact.")
    earlier(store, kind="rested", salience=0.6, text="Third local fact.")
    earlier(
        store,
        kind="job_delivered",
        place_key="NV/Reno",
        place="Reno",
        region="Nevada",
        text="Named destination fact.",
        activity="driving without a job",
        salience=0.1,
    )
    memory, _ = keeper(store)
    text = memory.context("Reno")
    assert len(text.splitlines()) == 4
    assert "First local fact" in text and "Named destination fact" in text
    assert "Third local fact" not in text


@pytest.mark.parametrize(
    "cue", ["remember", "last time", "before", "earlier", "previous", "been here"]
)
def test_cue_summaries_are_dated_and_capped(store, cue):
    for index in range(3):
        session = store.start_session("ets2", "other", NOW - timedelta(days=index + 1))
        store.end_session(session, f"Trip number {index}.", NOW)
    memory, _ = keeper(store, None)
    assert memory.context("hello") == ""
    text = memory.context(cue)
    assert text.count("Trip summary") == 2
    assert "Trip number 0" in text and "Trip number 1" in text
    assert "Trip number 2" not in text
    assert "today" not in text


def test_relative_dates_use_real_local_calendar(monkeypatch):
    import os
    import time
    from datetime import UTC, datetime

    old_tz = os.environ.get("TZ")
    monkeypatch.setenv("TZ", "America/Mexico_City")
    time.tzset()
    try:
        now = datetime(2026, 11, 16, 1, tzinfo=UTC)  # local November 15
        assert date_label("2026-11-16T00:00:00+00:00", now) == "2026-11-15 (today)"
        assert date_label("2026-11-15T01:00:00+00:00", now) == "2026-11-14 (yesterday)"
        assert date_label("2026-11-13T01:00:00+00:00", now) == "2026-11-12 (3 days ago)"
    finally:
        if old_tz is None:
            monkeypatch.delenv("TZ")
        if old_tz is not None:
            monkeypatch.setenv("TZ", old_tz)
        time.tzset()


def test_database_errors_never_escape(tmp_path):
    from familiar.memory import MemoryStore

    keeper = MemoryKeeper(
        MemoryStore(tmp_path / "m.db"),
        "ats",
        "mira",
        whereabouts=lambda: {"place_key": "TX/Austin", "place": "Austin"},
    )
    keeper.store.db.close()
    event = GameEvent(
        "passing_town",
        "ambient",
        {"summary": "x", "memory_salience": 0.5, "memory_arrival": True},
        NOW,
    )
    keeper.observe(event)
    assert keeper.maybe_recall(event) is None
    assert keeper.context("have we been here before?") == ""
