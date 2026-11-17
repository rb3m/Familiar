"""Offline actions: invented controls, fake keyboard and acquired telemetry samples."""

import os
from dataclasses import replace
from pathlib import Path
from time import monotonic
from unittest.mock import AsyncMock

import pytest
from familiar.interfaces import Actuator, ConfigError, KeyboardUser
from familiar_scs.actions import action_mixes, load_catalogue
from familiar_scs.adapter import AtsAdapter, Ets2Adapter
from familiar_scs.bindings import (
    DEFAULT_BINDINGS,
    GAME_NAMES,
    discover_controls,
    load_bindings,
    parse_controls,
)
from familiar_scs.telemetry import Snapshot

FIXTURE = Path(__file__).parent / "fixtures" / "controls_trimmed.sii"


class FakeKeyboard:
    def __init__(self):
        self.presses = []
        self.on_press = lambda: None

    async def tap(self, keys, hold_s=0.05):
        self.presses.append((tuple(keys), hold_s))
        self.on_press()

    async def aclose(self):
        pass


class FakeTelemetry:
    def __init__(self, *args):
        self.samples = []

    async def __aiter__(self):
        for sample in self.samples:
            yield sample

    async def aclose(self):
        pass


@pytest.fixture
async def adapter(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("familiar_scs.adapter.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("familiar_scs.adapter.READBACK_TIMEOUT_S", 0)
    monkeypatch.setattr("familiar_scs.adapter.HttpTelemetry", FakeTelemetry)
    instance = AtsAdapter({})
    instance.latest = Snapshot(connected=True)
    instance._watch_timestamp = monotonic()
    instance.attach_keyboard(FakeKeyboard())
    yield instance
    await instance.aclose()


def test_parser(caplog):
    with caplog.at_level("DEBUG"):
        bindings = parse_controls(FIXTURE.read_text())
    assert bindings == {
        "beacon": ("o",),
        "prv_ctrl": ("leftshift", "tab"),
        "ctrl_test": ("leftctrl", "2"),
        "alt_test": ("leftalt", "pagedown"),
        "plain_test": ("kpenter",),
        "wipers": ("p",),
        "engine": ("e",),
    }
    assert "mystery" in caplog.text and "space_laser" in caplog.text


@pytest.mark.parametrize(
    "scs,neutral",
    [
        ("a", "a"),
        ("z", "z"),
        ("key1", "1"),
        ("key0", "0"),
        ("lbracket", "leftbrace"),
        ("rbracket", "rightbrace"),
        ("lshift", "leftshift"),
        ("rshift", "rightshift"),
        ("lctrl", "leftctrl"),
        ("lalt", "leftalt"),
        ("uarrow", "up"),
        ("darrow", "down"),
        ("larrow", "left"),
        ("rarrow", "right"),
        ("pgup", "pageup"),
        ("pgdn", "pagedown"),
        ("num0", "kp0"),
        ("num9", "kp9"),
        ("numenter", "kpenter"),
        ("numplus", "kpplus"),
        ("numminus", "kpminus"),
        ("nummultiply", "kpasterisk"),
        ("numslash", "kpslash"),
        ("numperiod", "kpdot"),
        ("scrollock", "scrolllock"),
        ("del", "delete"),
        ("ins", "insert"),
        ("f1", "f1"),
        ("f12", "f12"),
        ("backslash", "backslash"),
    ],
)
def test_key_mapping(scs, neutral):
    assert parse_controls(f"mix test `keyboard.{scs}?0`") == {"test": (neutral,)}


@pytest.mark.parametrize(
    "platform,root,filename",
    [
        ("linux", ".local/share", "controls_linux.sii"),
        ("darwin", "Library/Application Support", "controls.sii"),
    ],
)
@pytest.mark.parametrize("game", ["ats", "ets2"])
def test_discovery(monkeypatch, tmp_path, platform, root, filename, game):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("familiar_scs.bindings.sys.platform", platform)
    base = tmp_path / root / GAME_NAMES[game]
    files = [
        base / store / name / filename
        for store, name in [("steam_profiles", "old"), ("profiles", "new")]
    ]
    for i, path in enumerate(files):
        path.parent.mkdir(parents=True)
        path.write_text(FIXTURE.read_text())
        os.utime(path, (100 + i, 100 + i))
    assert discover_controls(game) == files[1]
    assert discover_controls(game, "old") == files[0]
    assert discover_controls(game, str(files[0].parent)) == files[0]
    assert discover_controls(game, "missing") is None
    assert discover_controls(game, str(tmp_path / "missing")) is None


def test_overrides_and_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert load_bindings("ats", {}) == DEFAULT_BINDINGS
    (tmp_path / "controls_linux.sii").write_text(FIXTURE.read_text())
    monkeypatch.setattr("familiar_scs.bindings.sys.platform", "linux")
    settings = {
        "profile": str(tmp_path),
        "keys": {
            "beacon": "leftshift+o",
            "radio": "f5",
            "blinker_left": "leftctrl+a",
            "radio_station_next": "pagedown",
        },
    }
    bindings = load_bindings("ats", settings)
    assert bindings["beacon"] == ("leftshift", "o")
    assert bindings["radiotoggle"] == ("f5",)
    assert bindings["lblinker"] == ("leftctrl", "a")
    assert bindings["radionext"] == ("pagedown",)
    assert "hblight" not in bindings  # never fill missing profile mixes with defaults
    assert "rblinker" not in bindings


@pytest.mark.parametrize(
    "settings",
    [
        {"keys": {"beacon": "mystery"}},
        {"keys": {"bogus": "o"}},
        {"hold_s": {"engine": -1}},
        {"hold_s": {"engine": float("nan")}},
        {"hold_s": {"engine": True}},
        {"hold_s": {"bogus": 0.1}},
    ],
)
async def test_bad_config(monkeypatch, tmp_path, settings):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with pytest.raises(ConfigError):
        AtsAdapter(settings)


@pytest.mark.parametrize("cls", [AtsAdapter, Ets2Adapter])
async def test_protocols(monkeypatch, tmp_path, cls):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    instance = cls({})
    try:
        assert isinstance(instance, Actuator)
        assert isinstance(instance, KeyboardUser)
        assert set(instance.actions()) == set(action_mixes(load_catalogue()))
    finally:
        await instance.aclose()


@pytest.mark.parametrize(
    "name,field",
    [
        ("engine", "engine_on"),
        ("parking_brake", "park_brake_on"),
        ("cruise_control", "cruise_control_on"),
    ],
)
@pytest.mark.parametrize("state", ["on", "off"])
async def test_state_outcomes(adapter, name, field, state):
    desired = state == "on"
    adapter.latest = replace(adapter.latest, **{field: desired})
    assert (await adapter.perform(name, {"state": state}))["status"] == "already"
    assert not adapter._keyboard.presses
    adapter.latest = replace(adapter.latest, **{field: not desired})
    assert (await adapter.perform(name, {"state": state}))["status"] == "unknown"
    assert len(adapter._keyboard.presses) == 1

    def change_state():
        adapter.latest = replace(adapter.latest, **{field: desired})
        adapter._watch_timestamp = monotonic()

    adapter._keyboard.on_press = change_state
    assert (await adapter.perform(name, {"state": state}))["status"] == "done"
    assert len(adapter._keyboard.presses) == 2


@pytest.mark.parametrize(
    "guard,detail",
    [
        ("paused", "paused"),
        ("stale", "stale"),
        ("keyboard", "keyboard"),
        ("binding", "joystick-only"),
        ("connected", "disconnected"),
    ],
)
async def test_guards(adapter, guard, detail):
    keyboard = adapter._keyboard
    if guard in {"paused", "connected"}:
        adapter.latest = replace(adapter.latest, **{guard: guard == "paused"})
    if guard == "stale":
        adapter._watch_timestamp = monotonic() - 2
    if guard == "keyboard":
        adapter._keyboard = None
    if guard == "binding":
        del adapter.bindings["beacon"]
    result = await adapter.perform("beacon", {})
    assert result["status"] == "unavailable"
    assert detail in result["detail"].lower()
    assert not keyboard.presses


async def test_risk_and_context(adapter):
    specs = adapter.actions()
    assert specs["engine"].risky_when({"state": "off"})
    assert not specs["engine"].risky_when({"state": "on"})
    assert specs["parking_brake"].risky_when({"state": "on"})
    assert not specs["parking_brake"].risky_when({"state": "off"})
    assert specs["trailer_attach"].risky
    assert not specs["cruise_control"].risky
    before = adapter.action_context()
    adapter.latest = replace(adapter.latest, make="Invented", model="Truck")
    assert adapter.action_context() != before
    before = adapter.action_context()
    adapter.latest = replace(adapter.latest, on_job=True, cargo="Beans", destination_city="Town")
    assert adapter.action_context() != before
    before = adapter.action_context()
    adapter.latest = replace(adapter.latest, speed=70)
    assert adapter.action_context() == before


async def test_chords_and_holds(adapter):
    adapter.bindings["beacon"] = ("leftshift", "o")
    adapter._hold_s["beacon"] = 0.15
    result = await adapter.perform("beacon", {})
    assert result["status"] == "done" and "toggled" in result["detail"]
    assert adapter._keyboard.presses == [(("leftshift", "o"), 0.15)]
    await adapter.perform("horn", {})
    await adapter.perform("air_horn", {})
    await adapter.perform("light_cycle", {})
    assert adapter._keyboard.presses[-3:] == [(("h",), 0.6), (("n",), 0.6), (("l",), 0.08)]


@pytest.mark.parametrize(
    "name,args,mix",
    [
        (name, {}, mix)
        for name, mix in action_mixes(load_catalogue()).items()
        if isinstance(mix, str) and name not in {"engine", "parking_brake", "cruise_control"}
    ]
    + [
        ("blinker", {"side": "left"}, "lblinker"),
        ("blinker", {"side": "right"}, "rblinker"),
        ("retarder", {"direction": "up"}, "retarderup"),
        ("retarder", {"direction": "down"}, "retarderdown"),
        ("radio_station", {"direction": "next"}, "radionext"),
        ("radio_station", {"direction": "previous"}, "radioprev"),
    ],
)
async def test_action_routing(adapter, name, args, mix):
    adapter.bindings[mix] = ("f5",)
    result = await adapter.perform(name, args)
    assert result["status"] == "done"
    assert adapter._keyboard.presses[0][0] == ("f5",)
    if name == "wipers":
        assert result["wipers_on"] is False


async def test_post_press_disconnect(adapter):
    def disconnect():
        adapter.latest = Snapshot()

    adapter._keyboard.on_press = disconnect
    result = await adapter.perform("engine", {"state": "on"})
    assert result["status"] == "unknown"
    assert len(adapter._keyboard.presses) == 1


@pytest.mark.parametrize(
    "name,args",
    [
        ("bogus", {}),
        ("engine", {}),
        ("engine", {"state": "maybe"}),
        ("beacon", {"state": "on"}),
        ("blinker", {"side": "left", "extra": 1}),
    ],
)
async def test_invalid_actions(adapter, name, args):
    assert (await adapter.perform(name, args))["status"] == "error"
    assert not adapter._keyboard.presses


async def test_sample_acquisition_refreshes_guard(adapter):
    adapter._watch_timestamp = monotonic() - 5
    adapter.source.samples = [Snapshot(connected=True, engine_on=True)]
    async for _ in adapter.events():
        pass
    assert adapter.latest.engine_on
    assert monotonic() - adapter._watch_timestamp < 2
    assert (await adapter.perform("beacon", {}))["status"] == "done"


async def test_configured_keys_and_hold_reach_keyboard(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("familiar_scs.adapter.HttpTelemetry", FakeTelemetry)
    instance = Ets2Adapter({"keys": {"beacon": "leftshift+o"}, "hold_s": {"beacon": 0.2}})
    instance.attach_keyboard(FakeKeyboard())
    instance.latest = Snapshot(connected=True)
    assert (await instance.perform("beacon", {}))["status"] == "done"
    assert instance._keyboard.presses == [(("leftshift", "o"), 0.2)]
    await instance.aclose()


async def test_readback_waits_for_slow_telemetry(adapter, monkeypatch):
    """Telemetry polls about once a second, so a state change may land after the press."""
    monkeypatch.setattr("familiar_scs.adapter.READBACK_TIMEOUT_S", 60)
    polls = []

    async def poll(_delay):
        polls.append(_delay)
        if len(polls) == 3:
            adapter.latest = replace(adapter.latest, engine_on=True)
            adapter._watch_timestamp = monotonic()

    monkeypatch.setattr("familiar_scs.adapter.asyncio.sleep", poll)
    adapter.latest = replace(adapter.latest, engine_on=False)
    result = await adapter.perform("engine", {"state": "on"})
    assert result["status"] == "done"
    assert len(polls) == 3
