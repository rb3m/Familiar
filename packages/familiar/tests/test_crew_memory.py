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


def moment(memory, clock, *, age=timedelta(days=1), text="Blow up that bridge!"):
    now = clock.now
    clock.now -= age
    identifier = memory.remember_crew([("megumin", text), ("mira", "Please don't.")])
    clock.now = now
    return identifier


def test_crew_moment_storage_and_fact_isolation(store):
    memory, clock = keeper(store)
    identifier = moment(memory, clock)
    row = store.crew_moments("ets2")[0]
    assert row["id"] == identifier
    assert row["kind"] == "crew_moment" and row["source"] == "persona_claim"
    assert row["speaker"] == "megumin, mira"
    assert row["text"] == "megumin: Blow up that bridge!\nmira: Please don't."
    assert row["place"] == "Château-Thierry" and row["region"] == "France"
    assert row["ts_utc"] == (NOW - timedelta(days=1)).isoformat()
    current = store.start_session("ets2", "mira", NOW)
    assert store.episodes_at_place("ets2", row["place_key"], current) == []
    assert store.region_history("ets2", row["region"], current) == []
    assert store.search_terms("ets2", ["bridge"], current) == []
    assert store.known_terms("ets2") == set()
    with pytest.raises(ValueError, match="persona claims"):
        add(store, current, kind="crew_moment")


def test_multiple_moments_same_place_cap_prunes_oldest_and_recalls(store, monkeypatch):
    monkeypatch.setattr("familiar.memory.store.CREW_MOMENT_CAP", 2)
    memory, clock = keeper(store)
    other = MemoryKeeper(store, "ats", "mira", clock=clock)
    other.remember_crew([("mira", "Other game stays.")])
    oldest = moment(memory, clock, age=timedelta(days=3))
    store.record_recall(oldest, "mira", NOW)
    middle = moment(memory, clock, age=timedelta(days=2))
    newest = moment(memory, clock)
    assert [row["id"] for row in store.crew_moments("ets2")] == [newest, middle]
    assert len(store.crew_moments("ats")) == 1
    assert store.latest_recall() is None
    assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_moment_truncation_and_absent_location(store):
    memory, _ = keeper(store, None)
    memory.remember_crew([("mira", "A" * 500)])
    row = store.crew_moments("ets2")[0]
    assert row["text"] == "mira: " + "A" * 294
    assert row["place_key"] == row["region"] == row["activity"] == ""


@pytest.mark.parametrize(
    "age,eligible",
    [
        (timedelta(hours=11), False),
        (timedelta(hours=12), False),
        (timedelta(hours=12, seconds=1), True),
    ],
)
def test_callback_minimum_age(store, age, eligible):
    memory, clock = keeper(store)
    moment(memory, clock, age=age)
    assert bool(memory.crew_callback()) is eligible


@pytest.mark.parametrize("value,eligible", [(0, True), (0.249, True), (0.25, False), (1, False)])
def test_callback_injectable_probability(store, value, eligible):
    from types import SimpleNamespace

    memory, clock = keeper(store)
    moment(memory, clock)
    memory.rng = SimpleNamespace(random=lambda: value)
    assert bool(memory.crew_callback()) is eligible
    assert (store.latest_recall() is not None) is eligible


def test_callback_place_then_region_preference_and_prompt_label(store):
    from test_memory_keeper import LOCATION

    memory, clock = keeper(store)
    same_place = moment(memory, clock, age=timedelta(days=3), text="Same place bridge")
    memory.whereabouts = lambda: LOCATION | {"place_key": "FR/Paris", "place": "Paris"}
    regional = moment(memory, clock, age=timedelta(days=2), text="Regional bridge")
    memory.whereabouts = lambda: {"place_key": "US/Reno", "region": "Nevada"}
    moment(memory, clock, text="Unrelated bridge")
    memory.whereabouts = lambda: LOCATION
    prompt = memory.crew_callback()
    assert "Optional callback, use only if it fits naturally: on " in prompt
    assert "megumin, mira said:" in prompt and "Same place bridge" in prompt
    assert "This was banter, not fact." in prompt
    assert "2026-11-13 (3 days ago)" in prompt
    assert store.moment_last_use(same_place) == NOW
    clock.now += memory.cooldown
    assert "Regional bridge" in memory.crew_callback()
    assert store.moment_last_use(regional) == clock.now


def test_callback_shared_cooldown_and_seven_day_reuse_across_rosters(store):
    memory, clock = keeper(store)
    identifier = moment(memory, clock)
    assert memory.crew_callback()
    other = MemoryKeeper(store, "ets2", "frieren", clock=clock)
    other.rng = memory.rng
    assert other.crew_callback() == ""
    clock.now += timedelta(days=7) - timedelta(seconds=1)
    assert other.crew_callback() == ""
    clock.now += timedelta(seconds=1)
    assert other.crew_callback()
    assert store.last_recall(identifier, "frieren") == clock.now


def test_callback_fact_recall_shares_global_cooldown_and_game_isolation(store):
    _, fact = earlier(store)
    memory, clock = keeper(store)
    moment(memory, clock)
    store.record_recall(fact, "another", NOW)
    assert memory.crew_callback() == ""
    clock.now += memory.cooldown
    assert memory.crew_callback()
    assert memory.maybe_recall(event(arrival=True)) is None
    other = MemoryKeeper(store, "ats", "mira", clock=clock)
    clock.now += memory.cooldown
    assert other.crew_callback() == ""


@pytest.mark.parametrize("query", ["remember when Megumin wanted a bridge?", "have we joked?"])
async def test_player_recall_labels_crew_separately_in_model_prompt(store, tmp_path, crew, query):
    from test_crew_banter import chat

    earlier(store, text="A factual traffic jam.")
    memory, clock = keeper(store)
    moment(memory, clock)
    conversation = chat(tmp_path, crew)
    conversation.language = "en"
    conversation.memory = memory
    conversation.llm = LLM([[LLMChunk(text="She said that.")]])
    assert [line async for line in conversation.ask(query)]
    prompt = conversation.llm.requests[0][1]
    fact_block, crew_block = prompt.split("Crew memories", 1)
    assert "A factual traffic jam." in fact_block
    assert "Blow up" not in fact_block
    assert "crew said (not fact)" in crew_block
    assert "megumin: Blow up that bridge!" in crew_block
    assert "This was banter, not fact." in crew_block
    assert "Blow up" not in memory.context("hello")


@pytest.mark.parametrize("completed,ack", [(True, True), (True, False), (False, True)])
async def test_only_fully_heard_scene_stored(store, tmp_path, crew, completed, ack):
    from test_crew_banter import chat

    memory, _ = keeper(store)
    conversation = chat(tmp_path, crew)
    conversation.language = "en"
    conversation.memory = memory
    conversation.playback_ack_required = True
    scene = conversation.banter_scene()
    assert await anext(scene)
    acknowledge = {True: conversation.acknowledge_playback, False: lambda: None}[ack]
    acknowledge()
    if completed:
        await finish_scene(scene, acknowledge)
    await scene.aclose()
    rows = store.crew_moments("ets2")
    assert len(rows) == int(completed and ack)
    if rows:
        assert "mira: Hello." in rows[0]["text"]
        assert "frieren: Hi." in rows[0]["text"]


@pytest.mark.parametrize(
    "delay,speaker,stored", [(30, "mira", True), (31, "mira", False), (1, "frieren", False)]
)
async def test_direct_reply_to_heard_line_within_window(
    store, tmp_path, crew, delay, speaker, stored
):
    from test_crew_banter import chat

    memory, clock = keeper(store)
    conversation = chat(tmp_path, crew)
    conversation.language = "en"
    conversation.memory = memory
    scene = conversation.banter_scene()
    assert await anext(scene) == "Hello."
    assert await anext(scene) == "Hi."  # First line heard; second cancelled.
    await scene.aclose()
    assert not store.crew_moments("ets2")
    clock.now += timedelta(seconds=delay)
    conversation.llm = LLM([[LLMChunk(text="Yes.")]])
    assert [line async for line in conversation.ask(f"{speaker}, hello back.")]
    rows = store.crew_moments("ets2")
    assert len(rows) == int(stored)
    if rows:
        assert rows[0]["text"] == "mira: Hello."


async def test_full_scene_not_stored_twice_on_player_reply_and_callback_charged_on_offer(
    store, tmp_path, crew
):
    from test_crew_banter import chat, consume_scene

    memory, clock = keeper(store)
    identifier = moment(memory, clock)
    conversation = chat(tmp_path, crew)
    conversation.language = "en"
    conversation.memory = memory
    await consume_scene(conversation)
    assert "Optional callback" in conversation.llm.requests[0][1]
    assert store.moment_last_use(identifier) == NOW  # Even though Hello/Indeed ignore the hint.
    assert len(store.crew_moments("ets2")) == 2
    conversation.llm = LLM([[LLMChunk(text="Yes.")]])
    assert [line async for line in conversation.ask("Mira, hello.")]
    assert len(store.crew_moments("ets2")) == 2


def test_version_three_migration_preserves_claims_recalls_and_fact_deduplication(tmp_path):
    path = tmp_path / "v3.db"
    with sqlite3.connect(path) as db:
        db.executescript(_SCHEMA)
        db.execute("ALTER TABLE sessions ADD COLUMN speaker TEXT")
        db.execute("ALTER TABLE episodes ADD COLUMN speaker TEXT")
        db.execute("ALTER TABLE episodes ADD COLUMN source TEXT NOT NULL DEFAULT 'game'")
        db.execute("ALTER TABLE recalls ADD COLUMN speaker TEXT")
        db.execute("INSERT INTO schema_version VALUES (3)")
        db.execute(
            "INSERT INTO sessions VALUES (1, 'ets2', 'mira', ?, NULL, NULL, 'mira')",
            (NOW.isoformat(),),
        )
        db.execute(
            "INSERT INTO episodes VALUES "
            "(1, 1, 'ets2', ?, 'traffic_jam', 'x', 'X', '', '', 'Dragon!', 1, "
            "'megumin', 'persona_claim')",
            (NOW.isoformat(),),
        )
        db.execute("INSERT INTO recalls VALUES (1, 1, 'mira', ?, 'mira')", (NOW.isoformat(),))
    store = MemoryStore(path)
    assert store.last_recall(1, "mira") == NOW
    assert store.db.execute("SELECT source FROM episodes").fetchone()[0] == "persona_claim"
    assert store.db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert add(store, 1, place_key="x", source="game") == 1
    assert add(store, 1, place_key="x") is None
    memory, _ = keeper(store)
    memory.remember_crew([("mira", "One")])
    memory.remember_crew([("mira", "Two")])
    store.close()
    reopened = MemoryStore(path)
    assert len(reopened.crew_moments("ets2")) == 2
    assert reopened.last_recall(1, "mira") == NOW
    reopened.close()


async def finish_scene(scene, acknowledge):
    async for _ in scene:
        acknowledge()


@pytest.mark.parametrize("heard", [True, False])
async def test_direct_reply_to_regular_crew_line_requires_heard_playback(
    store, tmp_path, crew, heard
):
    from test_crew_banter import chat

    memory, _ = keeper(store)
    conversation = chat(tmp_path, crew)
    conversation.language = "en"
    conversation.memory = memory
    conversation.playback_ack_required = True
    conversation.llm = LLM([[LLMChunk(text="A dragon on that bridge!")]])
    acknowledge = {True: conversation.acknowledge_playback, False: lambda: None}[heard]
    await finish_scene(conversation.ask("Mira, what do you think?"), acknowledge)
    assert not store.crew_moments("ets2")
    conversation.llm = LLM([[LLMChunk(text="Only joking.")]])
    await finish_scene(conversation.ask("Mira, really?"), acknowledge)
    rows = store.crew_moments("ets2")
    assert len(rows) == int(heard)
    if rows:
        assert rows[0]["text"] == "mira: A dragon on that bridge!"


async def test_malformed_scene_does_not_store_a_moment(store, tmp_path, crew):
    from test_crew_banter import chat, consume_scene

    conversation = chat(tmp_path, crew, "mira: One speaker only.")
    conversation.memory = keeper(store)[0]
    assert not await consume_scene(conversation)
    assert not store.crew_moments("ets2")


async def test_closing_after_final_playback_ack_stores_fully_heard_scene(store, tmp_path, crew):
    from test_crew_banter import chat

    conversation = chat(tmp_path, crew, "mira: Hello.\nfrieren: Hi.")
    conversation.memory = keeper(store)[0]
    conversation.playback_ack_required = True
    scene = conversation.banter_scene()
    await anext(scene)
    conversation.acknowledge_playback()
    await anext(scene)
    conversation.acknowledge_playback()
    await scene.aclose()
    assert len(store.crew_moments("ets2")) == 1
    assert all(record.heard for record in conversation.records)


@pytest.mark.parametrize("kind", ["banter", "crew_moment"])
def test_crew_events_never_observed_as_facts(store, kind):
    memory, _ = keeper(store)
    memory.observe(event(kind=kind, salience=1))
    assert store.db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0


def test_new_crew_operations_fail_soft_when_database_closed(tmp_path):
    store = MemoryStore(tmp_path / "closed.db")
    memory, _ = keeper(store)
    store.close()
    assert memory.remember_crew([("mira", "Hello")]) is None
    assert memory.crew_callback() == ""
    assert memory.context("remember when") == ""
