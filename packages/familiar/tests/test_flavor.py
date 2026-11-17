"""Quote gating, durable cooldowns and speech admission."""

import random
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from familiar.config import FLAVOR_DEFAULTS, load_config
from familiar.conversation import Conversation
from familiar.flavor import FlavorKeeper
from familiar.interfaces import ConfigError, GameEvent, LLMChunk
from familiar.memory.store import SCHEMA_VERSION, MemoryStore

NOW = datetime(2026, 11, 16, tzinfo=UTC)
QUOTE = {
    "id": "shepard-light-this-candle",
    "text": "Let's light this candle",
    "speaker": "Alan Shepard",
    "context": "Freedom 7, 1961",
    "attribution": "verified",
}


def event(candidates=None, priority="notable"):
    data = {"summary": "Liftoff!"}
    if candidates is not None:
        data["flavor"] = candidates
    return GameEvent("liftoff", priority, data, NOW)


def keeper(store=None, **settings):
    return FlavorKeeper(
        store,
        "space",
        clock=lambda: NOW,
        rng=random.Random(1),
        settings={"probability": 1, **settings},
    )


def test_critical_and_missing_candidates_skip_without_roll():
    flavor = keeper()
    flavor.rng = Mock()
    assert flavor.choose(event([QUOTE], "critical")) is None
    assert flavor.choose(event()) is None
    assert flavor.choose(event([])) is None
    flavor.rng.random.assert_not_called()


@pytest.mark.parametrize(
    "probability,roll,selected",
    [(0, 0, False), (0.2, 0.2, False), (0.2, 0.19, True), (1, 0.99, True)],
)
def test_probability_gate(probability, roll, selected):
    flavor = keeper(probability=probability)
    flavor.rng = Mock()
    flavor.rng.random.return_value = roll
    flavor.rng.choice.side_effect = lambda candidates: candidates[0]
    assert (flavor.choose(event([QUOTE])) is not None) is selected


def test_global_cooldown_across_games_and_restart(tmp_path):
    path = tmp_path / "memory.db"
    store = MemoryStore(path)
    keeper(store).used("space", QUOTE)
    store.close()
    store = MemoryStore(path)
    flavor = keeper(store)
    flavor.game = "other-game"
    flavor.clock = lambda: NOW + timedelta(minutes=29)
    assert flavor.choose(event([QUOTE])) is None
    flavor.clock = lambda: NOW + timedelta(minutes=30)
    assert flavor.choose(event([QUOTE])) == QUOTE
    store.close()


def test_item_cooldown_survives_restart_and_is_scoped_by_game(tmp_path):
    path = tmp_path / "memory.db"
    store = MemoryStore(path)
    keeper(store).used("space", QUOTE)
    store.close()
    store = MemoryStore(path)
    flavor = keeper(store)
    flavor.clock = lambda: NOW + timedelta(days=13)
    assert flavor.choose(event([QUOTE])) is None
    flavor.game = "other-game"
    assert flavor.choose(event([QUOTE])) == QUOTE
    flavor.game = "space"
    flavor.clock = lambda: NOW + timedelta(days=14)
    assert flavor.choose(event([QUOTE])) == QUOTE
    store.close()


def test_uniform_choice_only_receives_eligible_candidates():
    flavor = keeper()
    flavor.used("space", QUOTE)
    flavor.clock = lambda: NOW + timedelta(hours=1)
    candidates = [dict(QUOTE, id="second"), dict(QUOTE, id="third")]
    flavor.rng = Mock()
    flavor.rng.random.return_value = 0
    flavor.rng.choice.return_value = candidates[1]
    assert flavor.choose(event([QUOTE, *candidates])) == candidates[1]
    flavor.rng.choice.assert_called_once_with(candidates)


def test_memory_disabled_tracks_global_and_item_uses():
    flavor = keeper()
    assert flavor.choose(event([QUOTE])) == QUOTE
    # Choosing is not using.
    assert flavor.choose(event([QUOTE])) == QUOTE
    flavor.used("space", QUOTE)
    assert flavor.choose(event([dict(QUOTE, id="second")])) is None
    flavor.clock = lambda: NOW + timedelta(minutes=30)
    assert flavor.choose(event([QUOTE])) is None
    assert flavor.choose(event([dict(QUOTE, id="second")])) is not None
    flavor.clock = lambda: NOW + timedelta(days=14)
    assert flavor.choose(event([QUOTE])) == QUOTE


def test_sqlite_errors_never_escape(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    flavor = keeper(store)
    store.close()
    assert flavor.choose(event([QUOTE])) is None
    flavor.used("space", QUOTE)


def test_v1_migration_preserves_memory(tmp_path):
    path = tmp_path / "memory.db"
    store = MemoryStore(path)
    session = store.start_session("space", "mira", NOW)
    with store.db:
        store.db.execute("DROP TABLE flavor_uses")
        store.db.execute("UPDATE schema_version SET version = 1")
    store.close()
    store = MemoryStore(path)
    assert store.db.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    assert store.db.execute("SELECT id FROM sessions").fetchone()[0] == session
    assert store.latest_flavor_use() is None
    store.record_flavor_use("space", QUOTE["id"], NOW)
    assert store.last_flavor_use("space", QUOTE["id"]) == NOW
    assert store.last_flavor_use("other", QUOTE["id"]) is None
    assert store.latest_flavor_use() == NOW
    store.close()


class LLM:
    def __init__(self, text="Ready for liftoff. Here we go."):
        self.text = text

    async def stream(self, messages, system, tools):
        self.messages = messages
        yield LLMChunk(text=self.text)


def conversation(flavor, text="Ready for liftoff. Here we go.", memory=None):
    persona = Mock()
    persona.system_prompt.return_value = "Be Mira."
    return Conversation(
        LLM(text), persona, "en", Mock(), flavor=flavor, game="space", memory=memory
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("attribution", ["verified", "attributed"])
async def test_hint_and_use_only_after_speech(attribution):
    flavor = keeper()
    flavor.used = Mock()
    candidate = dict(QUOTE, attribution=attribution)
    reply = conversation(flavor)
    spoken = False
    sentences = reply.interject(event([candidate]), spoken=lambda: spoken)
    await anext(sentences)
    hint = reply.llm.messages[-1].text
    assert "MAY loosely paraphrase" in hint
    assert "own voice" in hint
    assert "only if it fits" in hint
    assert "never quote it at length" in hint
    assert QUOTE["text"] in hint
    assert QUOTE["context"] in hint
    assert QUOTE["speaker"] in hint
    if attribution == "attributed":
        assert "as the line often credited to Alan Shepard goes" in hint
    flavor.used.assert_not_called()
    spoken = True
    await anext(sentences)
    flavor.used.assert_called_once_with("space", candidate)
    assert [sentence async for sentence in sentences] == []
    flavor.used.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "Ready for liftoff."])
async def test_unspoken_reply_does_not_record(text):
    flavor = keeper()
    flavor.used = Mock()
    reply = conversation(flavor, text)
    assert [
        sentence async for sentence in reply.interject(event([QUOTE]), spoken=lambda: False)
    ] == ([text] if text else [])
    flavor.used.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_before_speech_does_not_record():
    flavor = keeper()
    flavor.used = Mock()
    sentences = conversation(flavor).interject(event([QUOTE]))
    await anext(sentences)
    await sentences.aclose()
    flavor.used.assert_not_called()


@pytest.mark.asyncio
async def test_consumed_reply_records_memory_and_flavor_once():
    flavor = keeper()
    flavor.used = Mock()
    memory = Mock()
    incoming = event([QUOTE])
    incoming.data["memory_episode_id"] = 1
    reply = conversation(flavor, memory=memory)
    assert len([sentence async for sentence in reply.interject(incoming)]) == 2
    flavor.used.assert_called_once_with("space", QUOTE)
    memory.recalled.assert_called_once_with(incoming)


@pytest.mark.parametrize(
    "section",
    [
        "flavor = false",
        "[flavor]\nenabled = 1",
        "[flavor]\nprobability = -0.1",
        "[flavor]\nprobability = 1.1",
        "[flavor]\nprobability = true",
        "[flavor]\nprobability = nan",
        "[flavor]\ncooldown_min = inf",
        "[flavor]\ncooldown_min = -1",
        "[flavor]\nitem_cooldown_days = -1",
        '[flavor]\nitem_cooldown_days = "14"',
    ],
)
def test_invalid_config(tmp_path, section):
    path = tmp_path / "config.toml"
    path.write_text(section)
    with pytest.raises(ConfigError, match="flavor"):
        load_config(path)


def test_config_defaults_and_boundaries(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("")
    assert load_config(path).flavor == FLAVOR_DEFAULTS
    path.write_text(
        "[flavor]\nenabled = false\nprobability = 1\ncooldown_min = 0\nitem_cooldown_days = 0"
    )
    assert load_config(path).flavor == {
        "enabled": False,
        "probability": 1,
        "cooldown_min": 0,
        "item_cooldown_days": 0,
    }
