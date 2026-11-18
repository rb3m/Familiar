"""Attribution migration and fiction exclusion from factual memory."""

import sqlite3
from datetime import timedelta

import pytest
from familiar.interfaces import LLMChunk
from familiar.memory import MemoryKeeper, MemoryStore
from familiar.memory.store import _SCHEMA, SCHEMA_VERSION
from familiar.memory.summary import summarize
from familiar.transcript import TranscriptLog
from test_memory_keeper import earlier, event, keeper
from test_memory_store import NOW, add
from test_tools import LLM


@pytest.mark.parametrize("version", [1, 2])
def test_migrate_legacy_rows_once(tmp_path, version):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.executescript(_SCHEMA)
        db.execute("INSERT INTO schema_version VALUES (?)", (version,))
        db.execute(
            "INSERT INTO sessions VALUES (1, 'ats', 'mira', ?, NULL, NULL)", (NOW.isoformat(),)
        )
        db.execute(
            "INSERT INTO episodes VALUES "
            "(1, 1, 'ats', ?, 'landmark', 'x', 'X', '', '', 'Observed', 1)",
            (NOW.isoformat(),),
        )
        db.execute("INSERT INTO recalls VALUES (1, 1, 'mira', ?)", (NOW.isoformat(),))
    store = MemoryStore(path)
    assert store.db.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    row = dict(store.db.execute("SELECT * FROM episodes").fetchone())
    assert row["speaker"] == "mira" and row["source"] == "game"
    assert store.db.execute("SELECT speaker FROM recalls").fetchone()[0] == "mira"
    assert store.db.execute("SELECT speaker FROM sessions").fetchone()[0] == "mira"
    store.close()
    reopened = MemoryStore(path)
    assert reopened.db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 1
    reopened.close()


def test_claims_stored_but_never_retrieved_as_facts(store):
    old = store.start_session("ets2", "megumin", NOW)
    claim = add(
        store,
        old,
        text="A dragon caused the traffic jam.",
        source="persona_claim",
        speaker="megumin",
    )
    current = store.start_session("ets2", "mira", NOW)
    row = dict(store.db.execute("SELECT * FROM episodes WHERE id = ?", (claim,)).fetchone())
    assert row["source"] == "persona_claim" and row["speaker"] == "megumin"
    assert store.episodes_at_place("ets2", row["place_key"], current) == []
    assert store.region_history("ets2", "France", current) == []
    assert store.search_terms("ets2", ["dragon"], current) == []
    assert store.known_terms("ets2") == set()
    upgraded = add(
        store,
        old,
        text="The player saw stopped vehicles.",
        source="player_observed",
        speaker="player",
        place="Observed place",
        region="Observed region",
        activity="Observed activity",
    )  # A claim cannot block a later observed fact at the same place.
    facts = store.episodes_at_place("ets2", row["place_key"], current)
    assert len(facts) == 1 and facts[0]["source"] == "player_observed"
    assert upgraded == claim
    assert facts[0]["place"] == "Observed place"
    assert facts[0]["region"] == "Observed region"
    assert facts[0]["activity"] == "Observed activity"
    assert "dragon" not in facts[0]["text"]
    with pytest.raises(ValueError, match="Invalid memory source"):
        add(store, old, source="invented")


def test_game_observations_attribution_and_crew_cooldowns(store):
    _, episode = earlier(store)
    memory, clock = keeper(store)
    memory.crew = ("mira", "frieren")
    recall = memory.maybe_recall(event(arrival=True))
    memory.recalled(recall, speaker="frieren")
    clock.now += timedelta(minutes=45)
    assert not memory.recall_ready(episode)  # Seven-day gate is shared by this roster.
    other = MemoryKeeper(store, "ets2", "megumin", clock=clock, crew=("megumin",))
    assert other.recall_ready(episode)
    memory.observe(event())
    observed = dict(store.db.execute("SELECT * FROM episodes ORDER BY id DESC").fetchone())
    assert observed["source"] == "game" and observed["speaker"] is None


async def test_factual_summary_never_receives_persona_claims_or_unheard_text(tmp_path):
    log = TranscriptLog(tmp_path)
    log.record("user", text="I saw a mountain.", speaker_id="player", source="player_observed")
    log.record(
        "assistant",
        text="A dragon lives there!",
        speaker_id="megumin",
        source="persona_claim",
        heard=True,
    )
    log.record("user", text="I drove past it.", speaker_id="player", source="player_observed")
    log.record("assistant", text="Secret generated text.", speaker_id="frieren", heard=False)
    llm = LLM([[LLMChunk(text="The player saw a mountain.")]])
    assert await summarize(llm, log) == "The player saw a mountain."
    text = llm.requests[0][0][0].text
    assert "I saw a mountain." in text and "I drove past it." in text
    assert "dragon" not in text and "Secret generated text" not in text
