"""Controls and DJ roles (ADR 0018): who gets which tools, hand-off chains, session key."""

from dataclasses import asdict, replace

import pytest
import yaml
from familiar import session_settings as session
from familiar.crew import Crew
from familiar.hands import ActionKeeper
from familiar.interfaces import LLMChunk, ToolCall, ToolSpec
from familiar.tools import ToolRegistry
from test_crew import event
from test_crew_conversation import Actuator, consume, conversation

PLAY = ToolSpec("play_music", "Play music", {"type": "object", "properties": {}})
NOW = ToolSpec("now_playing", "Identify music", {"type": "object", "properties": {}})


def music_tools(played: list[str]) -> ToolRegistry:
    tools = ToolRegistry()

    async def play():
        played.append("play")
        return {"status": "playing"}

    async def now():
        return {"track": "Wild Dances"}

    tools.register(PLAY, play, role="dj", autonomous=True)
    tools.register(NOW, now)
    return tools


def split(crew: Crew, carrier: str = "mira", dj: str = "frieren") -> Crew:
    return Crew(crew.members, carrier, dj)


def names(specs) -> set[str]:
    return {spec.name for spec in specs}


def test_dj_defaults_to_carrier_and_roles_resolve(crew):
    alone = Crew(crew.members, "mira")
    assert alone.dj is alone.carrier
    assert alone.roles_of(alone.carrier) == {"controls", "dj"}
    roles = split(crew)
    frieren = roles.members[1]
    assert roles.roles_of(frieren) == {"dj"}
    assert roles.holder("carrier") is roles.carrier
    assert roles.holder("controls") is roles.carrier
    assert roles.holder("dj") is frieren
    assert roles.holder("frieren") is None


def test_dj_must_be_aboard(crew):
    with pytest.raises(ValueError, match="DJ"):
        Crew(crew.members[:2], "mira", "megumin")


def test_events_name_a_role(crew):
    roles = split(crew)
    dj_turn = replace(event("music_dj_turn"), data={"speaker": "dj"})
    assert roles.pick_for_event(dj_turn).id == "frieren"
    legacy = replace(event("music_dj_turn"), data={"speaker": "carrier"})
    assert roles.pick_for_event(legacy).id == "mira"
    critical = replace(event("hull", "critical"), data={"speaker": "dj"})
    assert roles.pick_for_event(critical).id == "mira"


def test_guest_turns_rotate_through_everyone_but_the_dj(crew):
    roles = split(crew)
    guest = replace(event("music_dj_turn"), data={"speaker": "guest"})
    assert [roles.pick_for_event(guest).id for _ in range(4)] == [
        "mira",
        "megumin",
        "mira",
        "megumin",
    ]
    alone = Crew(crew.members[:1], "mira")
    assert alone.pick_for_event(guest).id == "mira"


async def test_guest_turn_may_use_the_offered_dj_tool(tmp_path, crew):
    played = []
    roles = split(crew)
    chat = conversation(
        tmp_path,
        roles,
        [
            [LLMChunk(tool_call=ToolCall("p", "play_music", {}))],
            [LLMChunk(text="Explosions, obviously.")],
            [LLMChunk(tool_call=ToolCall("q", "play_music", {}))],
            [LLMChunk(text="Nothing.")],
        ],
        tools=music_tools(played),
    )
    megumin = replace(roles.members[2], tastes={"music": ["epic orchestral"]})
    roles.members = (roles.members[0], roles.members[1], megumin)
    base = event("music_dj_turn")
    extra = {"speaker": "guest", "speaker_id": "megumin", "tools": ["play_music"], "taste": "music"}
    guest = replace(base, data={**base.data, **extra})
    assert await consume(chat.interject(guest)) == ["Explosions, obviously."]
    assert played == ["play"]
    assert names(chat.llm.requests[0][2]) == {"play_music"}
    assert "epic orchestral" in chat.llm.requests[0][0][-1].text
    # Megumin again, without the guest grant: the DJ tool stays the DJ's.
    plain = replace(guest, data={**guest.data, "speaker": "dj"})
    await consume(chat.interject(plain))
    assert chat.llm.requests[2][2] == []
    assert played == ["play"]


def test_handoff_targets_skip_speaker_and_visited(crew):
    roles = split(crew)
    mira, frieren, megumin = roles.members
    both = {"controls", "dj"}
    assert set(roles.handoff_targets(megumin, both)) == {"mira", "frieren"}
    assert set(roles.handoff_targets(mira, both)) == {"frieren"}
    assert set(roles.handoff_targets(frieren, both, [megumin, mira])) == set()
    assert set(roles.handoff_targets(megumin, {"dj"})) == {"frieren"}
    assert roles.handoff_targets(roles.carrier, both, ()) == {"frieren": frieren}


def test_registry_roles_and_legacy_carrier_only():
    tools = ToolRegistry()
    handler = None
    tools.register(PLAY, handler, role="dj")
    tools.register(NOW, handler)
    tools.register(ToolSpec("act_gear", "Gear", {}), handler, carrier_only=True)
    assert tools.roles_in_use == {"dj", "controls"}
    assert names(tools.specs(())) == {"now_playing"}
    assert names(tools.specs({"dj"})) == {"play_music", "now_playing"}
    assert names(tools.specs({"controls"})) == {"act_gear", "now_playing"}
    assert len(tools.specs()) == 3
    with pytest.raises(ValueError, match="conflicts"):
        tools.register(ToolSpec("x", "", {}), handler, carrier_only=True, role="dj")
    with pytest.raises(ValueError, match="unknown role"):
        tools.register(ToolSpec("y", "", {}), handler, role="pilot")


async def test_carrier_hands_music_to_the_dj(tmp_path, crew):
    played = []
    roles = split(crew)
    chat = conversation(
        tmp_path,
        roles,
        [
            [LLMChunk(tool_call=ToolCall("h", "hand_off", {"line": "Ruslana's call."}))],
            [LLMChunk(tool_call=ToolCall("p", "play_music", {}))],
            [LLMChunk(text="Spinning now.")],
        ],
        tools=music_tools(played),
    )
    spoken = [(chat.speaker.id, line) async for line in chat.ask("Mira, play something")]
    assert spoken == [("mira", "Ruslana's call."), ("frieren", "Spinning now.")]
    assert played == ["play"]
    first, second = chat.llm.requests[0][2], chat.llm.requests[1][2]
    assert names(first) == {"now_playing", "hand_off"}
    handoff = next(spec for spec in first if spec.name == "hand_off")
    assert handoff.parameters["properties"]["to"]["enum"] == ["frieren"]
    assert "playing or changing music" in handoff.description
    assert names(second) == {"play_music", "now_playing"}  # no hand-off back to Mira
    assert "Only Frieren handles playing or changing music." in chat.llm.requests[0][1]
    assert "Only Mira handles operating the ship or game." in chat.llm.requests[1][1]


async def test_compound_request_chains_controls_then_music(tmp_path, crew):
    """Megumin hands to Mira; Mira lowers the gear and hands the music to Frieren."""
    played = []
    tools = music_tools(played)
    actuator = Actuator()
    actions = ActionKeeper(actuator, {}, "en")
    actions.register(tools)
    roles = split(crew)
    chat = conversation(
        tmp_path,
        roles,
        [
            [LLMChunk(tool_call=ToolCall("a", "hand_off", {"to": "mira"}))],
            [
                LLMChunk(tool_call=ToolCall("m", "act_move", {})),
                LLMChunk(tool_call=ToolCall("b", "hand_off", {"line": "Music's yours."})),
            ],
            [LLMChunk(tool_call=ToolCall("p", "play_music", {}))],
            [LLMChunk(text="Here we go.")],
        ],
        tools=tools,
        actions=actions,
    )
    spoken = [(chat.speaker.id, line) async for line in chat.ask("Megumin, move and play")]
    assert spoken == [("mira", "Music's yours."), ("frieren", "Here we go.")]
    assert actuator.performed == ["move"]
    assert played == ["play"]
    requests = chat.llm.requests
    megumin_handoff = next(spec for spec in requests[0][2] if spec.name == "hand_off")
    assert megumin_handoff.parameters["required"] == ["to"]
    assert set(megumin_handoff.parameters["properties"]["to"]["enum"]) == {"mira", "frieren"}
    mira_handoff = next(spec for spec in requests[1][2] if spec.name == "hand_off")
    assert mira_handoff.parameters["properties"]["to"]["enum"] == ["frieren"]
    assert "hand_off" not in names(requests[2][2])


async def test_one_member_with_both_roles_never_hands_off(tmp_path, crew):
    played = []
    roles = Crew(crew.members, "frieren", "frieren")
    chat = conversation(
        tmp_path,
        roles,
        [[LLMChunk(text="Frieren can.")]],
        tools=music_tools(played),
    )
    await consume(chat.ask("Megumin, play something"))
    assert "hand_off" in names(chat.llm.requests[0][2])
    chat.speaker = roles.carrier
    chat.llm.rounds = iter([[LLMChunk(text="On it.")]])
    await consume(chat.ask("Frieren, play something"))
    assert names(chat.llm.requests[1][2]) == {"play_music", "now_playing"}


async def test_dj_turn_offers_play_music_to_the_dj_only(tmp_path, crew):
    played = []
    roles = split(crew)
    chat = conversation(tmp_path, roles, [[], []], tools=music_tools(played))
    assert names(chat.tools.offered(["play_music"], roles.roles_of(roles.dj))) == {"play_music"}
    assert chat.tools.offered(["play_music"], roles.roles_of(roles.carrier)) == []


@pytest.fixture
def roster(tmp_path, persona):
    for identifier in ("test", "other"):
        member = replace(persona, id=identifier, display_name=identifier.title())
        (tmp_path / f"{identifier}.yaml").write_text(yaml.safe_dump(asdict(member)))
    return tmp_path


def write(path, extra=""):
    path.write_text(
        'version = 3\ncrew = ["test", "other"]\ncarrier = "test"\nlanguage = "en"\n'
        f"banter = 2\n{extra}"
    )


def read(path, roster, **overrides):
    return session.load(
        path, personas_dir=roster, user_personas_dir=roster, overrides=overrides or None
    )


def test_session_dj_round_trip_and_default(roster):
    path = roster / "session.toml"
    write(path)
    assert read(path, roster).dj == "test"
    write(path, 'dj = "other"\n')
    loaded = read(path, roster)
    assert loaded.dj == "other"
    session.save(path, loaded)
    assert 'dj = "other"' in path.read_text()


def test_session_dj_outside_crew_is_reported(roster):
    path = roster / "session.toml"
    write(path, 'dj = "ghost"\n')
    with pytest.raises(session.SessionError, match="dj must be a member of crew"):
        read(path, roster)


def test_persona_override_takes_both_roles(roster):
    path = roster / "session.toml"
    write(path, 'dj = "other"\n')
    loaded = read(path, roster, crew=("test",), carrier="test", dj="test")
    assert (loaded.carrier, loaded.dj) == ("test", "test")


@pytest.mark.parametrize("arguments", [{"to": []}, {"to": "ghost"}, {}])
async def test_bad_handoff_target_is_an_error_not_a_guess(tmp_path, crew, arguments):
    played = []
    tools = music_tools(played)
    actuator = Actuator()
    actions = ActionKeeper(actuator, {}, "en")
    actions.register(tools)
    chat = conversation(
        tmp_path,
        split(crew),
        [
            [LLMChunk(tool_call=ToolCall("h", "hand_off", arguments))],
            [LLMChunk(text="Hm.")],
        ],
        tools=tools,
        actions=actions,
    )
    spoken = [(chat.speaker.id, line) async for line in chat.ask("Megumin, play and move")]
    assert spoken == [("megumin", "Hm.")]
    assert played == [] and actuator.performed == []
    results = [message for message in chat.history if message.role == "tool"]
    assert len(results) == 1 and "Not handed off" in results[0].text


async def test_pending_confirmation_is_asked_before_handing_off(tmp_path, crew):
    played = []
    tools = music_tools(played)
    actuator = Actuator()
    actions = ActionKeeper(actuator, {}, "en")
    actions.register(tools)
    chat = conversation(
        tmp_path,
        split(crew),
        [
            [
                LLMChunk(tool_call=ToolCall("h", "hand_off", {"to": "frieren"})),
                LLMChunk(tool_call=ToolCall("s", "act_stage", {})),
            ],
            [LLMChunk(text="Stage now?")],
        ],
        tools=tools,
        actions=actions,
    )
    spoken = [(chat.speaker.id, line) async for line in chat.ask("Mira, stage and play music")]
    assert spoken == [("mira", "Stage now?")]
    assert actions.pending is not None and actuator.performed == []
    results = [message.text for message in chat.history if message.role == "tool"]
    assert "Not handed off: ask the player to confirm" in results[0]
    assert "needs_confirmation" in results[1]
