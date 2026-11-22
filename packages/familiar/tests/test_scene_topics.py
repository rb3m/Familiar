"""Scene cooldowns, optional source composition and prompt isolation."""

import random
from dataclasses import replace
from types import SimpleNamespace

import pytest
from familiar.banter import content_words, near_duplicate, opening_avoidance
from familiar.cli import _Runtime
from familiar.crew import persona_id
from familiar.history import HistoryRecord
from familiar.interfaces import LLMChunk, Message, SceneContext, SceneTopic
from familiar.topics import FREE_TOPIC, TUNING, TopicPicker
from test_crew import event
from test_crew_banter import chat
from test_tools import LLM


def test_family_cooldowns_and_persona_keys():
    now = 0
    picker = TopicPicker(lambda: now, random.Random(7))
    for family, cooldown in TUNING["cooldowns"].items():
        topic = SceneTopic(family, "a", "test")
        picker.note(family, "a")
        assert picker._eligible(topic) == (cooldown == 0)
    assert picker._eligible(SceneTopic("persona", "b", "other"))
    now = 1800
    assert picker._eligible(SceneTopic("persona", "a", "test"))
    assert not picker._eligible(SceneTopic("location", "b", "test"))
    now = 4500
    assert picker._eligible(SceneTopic("location", "b", "test"))
    assert not picker.history or picker.pick([]) == FREE_TOPIC
    assert picker.pick([], exclude=[FREE_TOPIC]) is None


@pytest.mark.parametrize(
    "kind,family",
    [
        ("music_track", "song"),
        ("music_dj_turn", "song"),
        ("music_other", "song"),
        ("weather:rain", "weather"),
        ("clock:period", "time"),
    ],
)
def test_event_notes(kind, family):
    picker = TopicPicker(lambda: 100, random.Random(0))
    picker.note_event(kind)
    assert list(picker.history) == [(family, kind, 100)]
    assert not picker._eligible(SceneTopic(family, "other", "new"))
    picker.note_event("combat")
    assert len(picker.history) == 1


def test_composition_and_error_isolation(caplog):
    def fail():
        raise RuntimeError("unavailable")

    topic = SceneTopic("time", "evening", "It is evening.")
    sources = [
        SimpleNamespace(scene_context=lambda: SceneContext("dusk", (topic,))),
        SimpleNamespace(describe=lambda: "Game facts"),
        SimpleNamespace(scene_context=fail),
    ]
    scene = _Runtime._scene_context(sources)
    assert scene == SceneContext("dusk", (topic,), "Game facts")
    assert "Scene context unavailable" in caplog.text


async def test_topic_prompt_hides_full_context_and_limits_history(tmp_path, crew):
    conversation = chat(tmp_path, crew)
    conversation.scene_context = lambda: SceneContext(
        "dusk; drizzle",
        (
            SceneTopic("song", "song", "SECRET SONG"),
            SceneTopic("location", "city", "SECRET CITY"),
        ),
    )
    conversation.picker.note("song", "song")
    conversation.picker.note("location", "city")
    conversation.records = [
        HistoryRecord("player", "player", f"record_{i}", True) for i in range(20)
    ]
    assert [line async for line in conversation.banter_scene()]
    prompt = conversation.llm.requests[0][1]
    assert "Background (do not recite): dusk; drizzle" in prompt
    assert f"Topic for this scene: {FREE_TOPIC.text}" in prompt
    assert "Do not mention the city or its nickname." in prompt
    assert "Do not mention the current song or artist." in prompt
    assert all(
        value not in prompt for value in ("SECRET SONG", "SECRET CITY", "Fuel: 10 L", "record_7")
    )
    assert "record_8" in prompt and "record_19" in prompt


async def test_event_frame_openings_and_exact_player_context(tmp_path, crew):
    conversation = chat(tmp_path, crew)
    conversation.language = "en"
    conversation.scene_context = lambda: SceneContext("late night", ())
    conversation.llm = LLM([[LLMChunk("Fresh reply.")], [LLMChunk("Exact reply.")]])
    conversation.records = [HistoryRecord("mira", "assistant", "*Da*, let us talk.", True)]
    cue = replace(
        event("music_track", "ambient"), data={"speaker": "controls", "summary": "SONG FACT"}
    )
    assert [line async for line in conversation.interject(cue)]
    messages, prompt, _ = conversation.llm.requests[0]
    assert "late night" in prompt and "Fuel: 10 L" not in prompt
    assert any("Do not open lines with: da let us" in message.text for message in messages)
    assert conversation.picker.history[-1][:2] == ("song", "music_track")
    assert [line async for line in conversation.ask("Mira, where?")]
    assert "Fuel: 10 L" in conversation.llm.requests[1][1]


def test_openings_and_similarity():
    records = [
        HistoryRecord("mira", "banter", text, True)
        for text in (
            "*Da*, let us start.",
            "Da let us finish.",
            "Now we wander.",
            "How curious indeed!",
        )
    ]
    records.append(HistoryRecord("frieren", "assistant", "Different speaker here.", True))
    assert opening_avoidance(records, "mira") == (
        "Do not open lines with: da let us; now we wander; how curious indeed"
    )
    assert near_duplicate(
        content_words("*Quiet* forests hold ancient secrets."),
        content_words("Quiet forests hold ancient stories."),
    )
    assert not near_duplicate(content_words("Da!"), content_words("Da!"))


@pytest.mark.parametrize("retry_good", [True, False])
async def test_duplicate_retry_once_with_new_topic_then_skip(tmp_path, crew, retry_good, caplog):
    conversation = chat(tmp_path, crew)
    now = 0
    conversation.clock = lambda: now
    conversation.picker = TopicPicker(lambda: now, random.Random(0))
    conversation.scene_context = lambda: SceneContext(
        "quiet", (SceneTopic("persona", "mira:forest", "Mira likes talking about forests"),)
    )
    conversation._record_message(Message("assistant", "Quiet forests hold ancient secrets."))
    duplicate = "mira: Quiet forests hold ancient stories.\nfrieren: Curious creatures wander."
    fresh = "mira: Lanterns sparkle brightly tonight.\nfrieren: Shall we invent adventures?"
    second = {True: fresh, False: duplicate}[retry_good]
    conversation.llm = LLM([[LLMChunk(duplicate)], [LLMChunk(second)]])
    with caplog.at_level("DEBUG"):
        lines = [line async for line in conversation.banter_scene()]
    assert bool(lines) == retry_good
    assert len(conversation.llm.requests) == 2
    prompts = [
        request[1].split("Topic for this scene: ")[1].splitlines()[0]
        for request in conversation.llm.requests
    ]
    assert prompts[0] != prompts[1]
    assert "near-duplicate" in caplog.text
    now = 2700
    assert not conversation._repeats([("mira", "Quiet forests hold ancient secrets.")])


def test_persona_topic_composition(tmp_path, crew):
    crew.members = (replace(crew.members[0], topics=["lanterns"]), *crew.members[1:])
    conversation = chat(tmp_path, crew)
    topic = conversation._scene_topics(SceneContext("", ()))[0]
    assert topic == SceneTopic("persona", "mira:lanterns", "Mira likes talking about lanterns")


async def test_banter_openings_are_attributed_to_each_speaker(tmp_path, crew):
    conversation = chat(tmp_path, crew)
    conversation.records = [
        HistoryRecord("mira", "assistant", "*Da*, let us talk.", True),
        HistoryRecord("frieren", "banter", "Ancient trees remember everything.", True),
    ]
    assert [line async for line in conversation.banter_scene()]
    prompt = conversation.llm.requests[0][1]
    assert "mira: Do not open lines with: da let us" in prompt
    assert "frieren: Do not open lines with: ancient trees remember" in prompt


async def test_streamed_event_repetition_is_not_rejected(tmp_path, crew):
    conversation = chat(tmp_path, crew)
    conversation.language = "en"
    conversation._record_message(Message("assistant", "Quiet forests hold ancient secrets."))
    conversation.llm = LLM([[LLMChunk("Quiet forests hold ancient secrets.")]])
    cue = replace(event(), data={"speaker": "controls", "summary": "A grounded event."})
    assert [line async for line in conversation.interject(cue)] == [
        "Quiet forests hold ancient secrets."
    ]
    assert len(conversation.llm.requests) == 1


def test_openings_only_use_last_eight_speaker_lines():
    records = [HistoryRecord("mira", "assistant", "Old repeated opening.", True)] * 2
    records += [
        HistoryRecord("mira", "banter", f"Fresh line number {index}.", True) for index in range(8)
    ]
    assert "old repeated opening" not in opening_avoidance(records, "mira")


def test_multiline_assistant_records_feed_guards(tmp_path, crew):
    conversation = chat(tmp_path, crew)
    conversation._record_message(
        Message("assistant", "Quiet forests hold ancient secrets.\nLanterns glow brightly tonight.")
    )
    assert conversation._repeats([("frieren", "Quiet forests hold ancient secrets.")])
    assert opening_avoidance(conversation.records, "mira") == (
        "Do not open lines with: quiet forests hold; lanterns glow brightly"
    )


async def test_chosen_topic_is_not_forbidden(tmp_path, crew):
    conversation = chat(tmp_path, crew)
    scene = SceneContext(
        "dusk",
        (SceneTopic("song", "song", "Now playing X."), SceneTopic("location", "city", "Big Q")),
        "Game facts",
    )
    text = conversation._scene_background(scene, scene.topics[0])
    assert "Current situation: Game facts" in text
    assert "Topic for this scene: Now playing X." in text
    assert "Do not mention the city or its nickname." in text
    assert "current song" not in text


def test_persona_topics_share_one_weight_per_persona():
    picker = TopicPicker(lambda: 0, random.Random(1))
    many = [SceneTopic("persona", f"ruslana:{n}", str(n)) for n in range(10)]
    weather = SceneTopic("weather", "weather", "Drizzle.")
    picks = []
    for _ in range(2000):
        picker.history.clear()
        picks.append(picker.pick([*many, weather]).family)
    # persona 3 vs weather 1 vs free 1.5: about 0.18 weather, not 1/14.5.
    assert 0.14 < picks.count("weather") / len(picks) < 0.22


def test_scene_topics_only_from_the_speaking_pair(tmp_path, crew):
    first, second, *rest = crew.members
    crew.members = (replace(first, topics=["lanterns"]), replace(second, topics=["toads"]), *rest)
    conversation = chat(tmp_path, crew)
    topics = conversation._scene_topics(SceneContext("", ()), (persona_id(first), "nobody"))
    assert [topic.text for topic in topics] == [
        f"{first.display_name} likes talking about lanterns"
    ]
