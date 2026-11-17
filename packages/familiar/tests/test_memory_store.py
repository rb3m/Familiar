"""Persistent trip facts, game isolation and indexed recall history."""

from datetime import UTC, datetime, timedelta

import pytest
from familiar.memory.store import SCHEMA_VERSION, MemoryStore, default_path

NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)


def add(store, session, game="ets2", **fields):
    values = dict(
        kind="traffic_jam",
        place_key="FR/Château-Thierry",
        place="Château-Thierry",
        region="France",
        activity="hauling 18 tons of cattle to Bayonne",
        text="Traffic jam on the motorway.",
        salience=0.5,
        now=NOW,
    )
    return store.add_episode(session, game, **(values | fields))


def test_session_episode_crud_and_reopen(tmp_path):
    path = tmp_path / "nested/memory.db"
    store = MemoryStore(path)
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.db.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    session = store.start_session("ets2", "mira", NOW)
    identifier = add(store, session)
    assert add(store, session) is None
    assert store.revision == 1
    store.end_session(session, "We hauled cattle.", NOW + timedelta(hours=1))
    store.close()
    store = MemoryStore(path)
    current = store.start_session("ets2", "other", NOW + timedelta(days=1))
    rows = store.episodes_at_place("ets2", "FR/Château-Thierry", current)
    assert len(rows) == 1 and rows[0]["id"] == identifier
    assert rows[0]["activity"].endswith("to Bayonne")
    summaries = store.latest_summaries("ets2", current)
    assert summaries[0]["summary"] == "We hauled cattle."
    assert summaries[0]["ended_utc"] is not None
    assert store.episodes_at_place("ets2", "FR/Château-Thierry", session) == []
    assert store.episodes_at_place("ats", "FR/Château-Thierry", current) == []
    store.close()


def test_queries_terms_salience_region_and_order(store):
    old = store.start_session("ets2", "mira", NOW)
    low = add(store, old, kind="passing_town", salience=0.1)
    high = add(store, old, kind="collision", salience=0.8)
    other = store.start_session("ats", "mira", NOW)
    add(store, other, game="ats")
    current = store.start_session("ets2", "mira", NOW)
    add(store, current, kind="fined", salience=0.9)
    rows = store.episodes_at_place("ets2", "FR/Château-Thierry", current, min_salience=0.5)
    assert [row["id"] for row in rows] == [high]
    assert [row["id"] for row in store.region_history("ets2", "France", current)] == [high, low]
    assert len(store.search_terms("ets2", ["CHATEAU thierry"], current)) == 2
    for term in ("france", "cattle", "Bayonne", "motorway"):
        assert store.search_terms("ets2", [term], current, limit=1)[0]["id"] == high
    assert store.search_terms("ets2", ["catt"], current) == []
    assert store.search_terms("ets2", [], current) == []
    assert store.known_terms("ets2") == {"chateau thierry", "france", "cattle", "bayonne"}


def test_recall_persistence_persona_scope_and_latest(store, tmp_path):
    session = store.start_session("ets2", "mira", NOW)
    episode = add(store, session)
    assert store.last_recall(episode, "mira") is None
    assert store.latest_recall() is None
    store.record_recall(episode, "mira", NOW)
    store.record_recall(episode, "other", NOW + timedelta(hours=1))
    assert store.last_recall(episode, "mira") == NOW
    assert store.last_recall(episode, "new") is None
    assert store.latest_recall() == NOW + timedelta(hours=1)
    reopened = MemoryStore(tmp_path / "memory.db")
    assert reopened.last_recall(episode, "mira") == NOW
    reopened.close()


def test_default_path(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert default_path() == tmp_path / "xdg/familiar/memory.db"
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_path() == tmp_path / ".local/share/familiar/memory.db"


def test_unknown_schema_is_rejected(tmp_path):
    path = tmp_path / "memory.db"
    store = MemoryStore(path)
    with store.db:
        store.db.execute("UPDATE schema_version SET version = 999")
    store.close()
    with pytest.raises(ValueError, match="Unsupported memory schema"):
        MemoryStore(path)
