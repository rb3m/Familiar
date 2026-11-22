"""Moment-bound service events: cue timing, carrier routing, offered tools and tastes."""

import random
from dataclasses import asdict, replace
from datetime import UTC, datetime

import pytest
import yaml
from familiar.crew import Crew, persona_id
from familiar.director import Director
from familiar.interfaces import ConfigError, GameEvent, LLMChunk, ToolCall, ToolSpec
from familiar.persona import load_persona
from familiar.tools import ToolRegistry
from test_crew_conversation import consume, conversation

OPEN = {"type": "object", "properties": {}, "additionalProperties": False}


def cue(kind="music_track", **data):
    return GameEvent(
        kind,
        "ambient",
        {"summary": "Now playing: Song.", "companion_service": True, "cue": True, **data},
        datetime.now(UTC),
    )


def test_cue_skips_quiet_gaps_but_waits_for_silence():
    director = Director(0.5, {})
    director.notify_spoke()  # someone just spoke: an ordinary ambient event must wait
    plain = cue()
    plain.data.pop("cue")
    director.submit(replace(plain, kind="ambient_thing"))
    assert director._take() is None
    director.set_busy("speaking", True)
    director.submit(cue())
    assert director._take() is None  # never talks over a line in progress
    director.set_busy("speaking", False)
    assert director._take().kind == "music_track"


def test_speaker_carrier_routes_to_the_carrier(crew):
    # Even a chattier member keen on the event kind does not take the carrier's job.
    keen = replace(crew.members[1], chattiness=1.0, interests={"music_dj_turn": 5})
    crew = Crew([crew.carrier, keen], persona_id(crew.carrier))
    assert crew.pick_for_event(cue("music_dj_turn"), random.Random(0)) is keen
    assert crew.pick_for_event(cue("music_dj_turn", speaker="carrier")) is crew.carrier


def registry():
    tools = ToolRegistry()

    async def handler():
        return {"ok": True}

    tools.register(
        ToolSpec("play_music", "Play", OPEN), handler, carrier_only=True, autonomous=True
    )
    tools.register(ToolSpec("skip_track", "Skip", OPEN), handler, carrier_only=True)
    tools.register(ToolSpec("now_playing", "Now", OPEN), handler, autonomous=True)
    return tools


def test_events_offer_only_named_autonomous_tools():
    tools = registry()
    names = ["play_music", "skip_track", "missing"]
    assert [spec.name for spec in tools.offered(names, {"controls"})] == ["play_music"]
    assert tools.offered(names, ()) == []  # role tools stay with their role
    assert tools.offered([], {"controls"}) == []


async def test_dj_turn_offers_play_music_to_the_carrier_with_taste(tmp_path, crew):
    carrier = replace(crew.carrier, tastes={"music": ["70s city pop", "quiet folk"]})
    crew = Crew([carrier, *crew.members[1:]], persona_id(carrier))
    chat = conversation(
        tmp_path,
        crew,
        [
            [LLMChunk(tool_call=ToolCall("a", "play_music", {}))],
            [LLMChunk(text="Some city pop for the long haul.")],
        ],
        tools=registry(),
    )
    event = cue("music_dj_turn", speaker="carrier", tools=["play_music"], taste="music")
    lines = await consume(chat.interject(event))
    assert lines == ["Some city pop for the long haul."]
    assert chat.speaker is carrier
    messages, _, specs = chat.llm.requests[0]
    assert [spec.name for spec in specs] == ["play_music"]
    assert "Your taste (hints, not rules): 70s city pop; quiet folk" in messages[-1].text


def test_persona_tastes_are_validated(tmp_path, persona):
    good = {**asdict(persona), "tastes": {"music": ["jazz"]}}
    (tmp_path / "test.yaml").write_text(yaml.safe_dump(good))
    assert load_persona(tmp_path, "test").tastes == {"music": ["jazz"]}
    for bad in ({"music": "jazz"}, {"music": [""]}, ["jazz"]):
        (tmp_path / "test.yaml").write_text(yaml.safe_dump({**good, "tastes": bad}))
        with pytest.raises(ConfigError, match="tastes"):
            load_persona(tmp_path, "test")
