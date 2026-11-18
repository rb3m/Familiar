"""Keyboard actions never retry and report only observed game outcomes."""

import asyncio
import os

import pytest
from familiar.interfaces import Actuator, ConfigError, KeyboardUser, load_data
from familiar_elite import adapter as adapter_module
from familiar_elite import bindings
from familiar_elite.actions import BindingSource, hold_times, load_catalogue
from familiar_elite.adapter import EliteAdapter
from familiar_elite.paths import ElitePaths


class FakeStatus:
    def __init__(self):
        self.flags = {
            "in_main_ship": True,
            "on_foot": False,
            "landing_gear_down": False,
            "lights_on": False,
            "cargo_scoop_deployed": False,
            "night_vision": False,
        }
        self.gui_focus = 0
        self.pips = [4, 4, 4]
        self.is_fresh = True

    def fresh(self):
        return self.is_fresh

    def poll(self):
        pass


class FakeKeyboard:
    def __init__(self):
        self.taps = []
        self.on_tap = lambda: None

    async def tap(self, keys, hold_s=0.05):
        self.taps.append((tuple(keys), hold_s))
        self.on_tap()

    async def aclose(self):
        pass


def _remove_waits(entry):
    entry["steps"] = [dict(step, wait_s=0) for step in entry["steps"]]


@pytest.fixture
def hands(monkeypatch, tmp_path):
    async def fake_io(function, *args, **kwargs):
        # All action I/O in this fixture is fake; no executor threads are needed.
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", fake_io)
    monkeypatch.setenv("FAMILIAR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(adapter_module, "READBACK_TIMEOUT_S", 0.02)
    monkeypatch.setattr(adapter_module, "DOCKING_TIMEOUT_S", 0.02)
    monkeypatch.setattr(adapter_module, "READBACK_STEP_S", 0.001)
    monkeypatch.setattr(adapter_module, "find_paths", lambda settings: ElitePaths())
    adapter = EliteAdapter({"hold_s": {"landing_gear": 0.12}})
    adapter.status = FakeStatus()
    adapter.state.commander, adapter.state.ship_id = "Mira", 42
    keyboard = FakeKeyboard()
    adapter.attach_keyboard(keyboard)
    table = {
        "LandingGearToggle": ("g",),
        "ShipSpotLightToggle": ("l",),
        "ToggleCargoScoop": ("c",),
        "NightVisionToggle": ("n",),
        "HumanoidToggleNightVisionButton": ("h",),
        "ResetPowerDistribution": ("r",),
        "IncreaseSystemsPower": ("s",),
        "FocusLeftPanel": ("1",),
        "CyclePreviousPanel": ("q",),
        "CycleNextPanel": ("e",),
        "UI_Select": ("space",),
        "UI_Right": ("d",),
    }
    monkeypatch.setattr(adapter._binding_source, "refresh", lambda paths: table)
    # Exercise the shipped controls in order while avoiding real macro waits.
    sequences = (entry for entry in adapter._catalogue.values() if "steps" in entry)
    for entry in sequences:
        _remove_waits(entry)
    return adapter, keyboard, table


def test_protocols_and_specs(hands):
    adapter, _, _ = hands
    assert isinstance(adapter, Actuator)
    assert isinstance(adapter, KeyboardUser)
    assert adapter.action_context() == "Mira:42"
    assert len(adapter.actions()) == 10
    assert all(not spec.risky for spec in adapter.actions().values())
    assert adapter.actions()["landing_gear"].parameters["properties"]["state"]["enum"] == [
        "up",
        "down",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,flag,state",
    [
        ("landing_gear", "landing_gear_down", "down"),
        ("lights", "lights_on", "on"),
        ("cargo_scoop", "cargo_scoop_deployed", "open"),
        ("night_vision", "night_vision", "on"),
    ],
)
async def test_toggle_already_and_done(hands, name, flag, state):
    adapter, keyboard, _ = hands
    adapter.status.flags[flag] = True
    assert (await adapter.perform(name, {"state": state}))["status"] == "already"
    assert not keyboard.taps
    adapter.status.flags[flag] = False
    keyboard.on_tap = lambda: adapter.status.flags.update({flag: True})
    assert (await adapter.perform(name, {"state": state}))["status"] == "done"
    assert len(keyboard.taps) == 1


@pytest.mark.asyncio
async def test_toggle_unknown_and_hold(hands):
    adapter, keyboard, _ = hands
    result = await adapter.perform("landing_gear", {"state": "down"})
    assert result["status"] == "unknown"
    assert keyboard.taps == [(("g",), 0.12)]


@pytest.mark.asyncio
async def test_toggle_delayed_readback(hands):
    adapter, keyboard, _ = hands
    adapter.status.flags["landing_gear_down"] = True
    adapter.status.poll = lambda: adapter.status.flags.update(landing_gear_down=False)
    assert (await adapter.perform("landing_gear", {"state": "up"}))["status"] == "done"
    assert len(keyboard.taps) == 1


@pytest.mark.asyncio
async def test_on_foot_night_vision(hands):
    adapter, keyboard, _ = hands
    adapter.status.flags.update(on_foot=True, in_main_ship=False)
    keyboard.on_tap = lambda: adapter.status.flags.update(night_vision=True)
    assert (await adapter.perform("night_vision", {"state": "on"}))["status"] == "done"
    assert keyboard.taps == [(("h",), 0.08)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "guard,detail",
    [
        ("stale", "stale"),
        ("shutdown", "shut down"),
        ("focus", "GuiFocus"),
        ("on_foot", "main ship"),
        ("srv", "main ship"),
        ("keyboard", "keyboard"),
        ("missing_key", "LandingGearToggle has no keyboard key"),
    ],
)
async def test_guards(hands, guard, detail):
    adapter, keyboard, table = hands
    changes = {
        "stale": lambda: setattr(adapter.status, "is_fresh", False),
        "shutdown": lambda: setattr(adapter.state, "shutdown", True),
        "focus": lambda: setattr(adapter.status, "gui_focus", 1),
        "on_foot": lambda: adapter.status.flags.update(on_foot=True),
        "srv": lambda: adapter.status.flags.update(in_main_ship=False),
        "keyboard": lambda: setattr(adapter, "_keyboard", None),
        "missing_key": lambda: table.pop("LandingGearToggle"),
    }
    changes[guard]()
    result = await adapter.perform("landing_gear", {"state": "down"})
    assert result["status"] == "unavailable"
    assert detail in result["detail"]
    assert not keyboard.taps


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,pips,keys",
    [
        ("balance_power", [4, 4, 4], ["r"]),
        ("shields_priority", [8, 2, 2], ["r", "s", "s"]),
    ],
)
async def test_power_verification(hands, name, pips, keys):
    adapter, keyboard, _ = hands
    adapter.status.pips = [0, 6, 6]
    keyboard.on_tap = lambda: setattr(adapter.status, "pips", pips)
    assert (await adapter.perform(name, {}))["status"] == "done"
    assert [tap[0][0] for tap in keyboard.taps] == keys


@pytest.mark.asyncio
async def test_power_unverified_and_preflight(hands):
    adapter, keyboard, table = hands
    table.pop("IncreaseSystemsPower")
    assert (await adapter.perform("shields_priority", {}))["status"] == "unavailable"
    assert not keyboard.taps
    assert (await adapter.perform("shields_priority", {}))["status"] == "unavailable"
    table["IncreaseSystemsPower"] = ("s",)
    assert (await adapter.perform("shields_priority", {}))["status"] == "unknown"
    assert len(keyboard.taps) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event,status,detail",
    [
        ({"event": "DockingGranted", "LandingPad": 12}, "done", "pad 12"),
        ({"event": "DockingDenied", "Reason": "NoSpace"}, "done", "NoSpace"),
        ({"event": "DockingTimeout"}, "done", "DockingTimeout"),
        ({"event": "DockingCancelled"}, "done", "DockingCancelled"),
        ({"event": "DockingRequested"}, "unknown", "not repeated"),
        (None, "unknown", "not repeated"),
    ],
)
async def test_docking_journal_outcomes(hands, event, status, detail):
    adapter, keyboard, _ = hands
    adapter.state.apply({"event": "DockingGranted", "LandingPad": 99})

    def emit():
        if event is not None:
            adapter.state.apply(event)

    keyboard.on_tap = emit
    result = await adapter.perform("request_docking", {})
    assert result["status"] == status
    assert detail in result["detail"]
    assert [tap[0][0] for tap in keyboard.taps] == [
        "1",
        "q",
        "q",
        "q",
        "q",
        "e",
        "e",
        "space",
        "d",
        "space",
        "1",
    ]


@pytest.mark.asyncio
async def test_docking_requested_then_granted(hands):
    adapter, keyboard, _ = hands
    keyboard.on_tap = lambda: adapter.state.apply({"event": "DockingRequested"})

    async def grant():
        await asyncio.sleep(0.005)
        adapter.state.apply({"event": "DockingGranted", "LandingPad": 7})

    task = asyncio.create_task(grant())
    result = await adapter.perform("request_docking", {})
    await task
    assert result["status"] == "done"
    assert result["landing_pad"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["docked", "supercruise", "on_foot", "focus"])
async def test_docking_guards(hands, field):
    adapter, keyboard, _ = hands
    if field == "focus":
        adapter.status.gui_focus = 1
    if field != "focus":
        adapter.status.flags[field] = True
    assert (await adapter.perform("request_docking", {}))["status"] == "unavailable"
    assert not keyboard.taps


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,args",
    [
        ("missing", {}),
        ("lights", {"state": "maybe"}),
        ("lights", {}),
        ("balance_power", {"state": "on"}),
    ],
)
async def test_invalid_arguments(hands, name, args):
    adapter, keyboard, _ = hands
    assert (await adapter.perform(name, args))["status"] == "error"
    assert not keyboard.taps


def _preset(path, key):
    path.write_text(
        '<Root><LandingGearToggle><Primary Device="Keyboard" '
        f'Key="{key}" /></LandingGearToggle></Root>'
    )


def test_keymap_reload_and_sidecar_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("FAMILIAR_DATA_DIR", str(tmp_path / "data"))
    start = tmp_path / "StartPreset.4.start"
    start.write_text("Custom\n")
    preset = tmp_path / "Custom.4.0.binds"
    _preset(preset, "Key_G")
    monkeypatch.setattr(bindings, "check_sidecar", lambda presets, path: ["owned keys changed"])
    paths = ElitePaths(bindings_dir=tmp_path)
    source = BindingSource(paths)
    assert "owned keys changed" in caplog.text
    assert source.table["LandingGearToggle"] == ("g",)
    stamp = preset.stat().st_mtime_ns
    _preset(preset, "Key_L")
    os.utime(preset, ns=(stamp + 1_000_000, stamp + 1_000_000))
    assert source.refresh(paths)["LandingGearToggle"] == ("l",)
    other = tmp_path / "Other.4.0.binds"
    _preset(other, "Key_N")
    start.write_text("Other\n")
    assert source.refresh(paths)["LandingGearToggle"] == ("n",)
    other.write_text("torn XML")
    assert source.refresh(paths) == {}


def test_hold_validation_and_data_override(tmp_path, monkeypatch):
    monkeypatch.setenv("FAMILIAR_DATA_DIR", str(tmp_path))
    directory = tmp_path / "familiar_elite"
    directory.mkdir()
    (directory / "actions.yaml").write_text("lights:\n  hold_s: 0.3\n")
    catalogue = load_catalogue()
    assert catalogue["lights"]["hold_s"] == 0.3
    assert hold_times(catalogue, {"lights": 0.4})["lights"] == 0.4
    for overrides in ({"unknown": 0.1}, {"lights": -1}, {"lights": True}):
        with pytest.raises(ConfigError):
            hold_times(catalogue, overrides)
    assert isinstance(load_data("familiar_elite", "actions").value, dict)
