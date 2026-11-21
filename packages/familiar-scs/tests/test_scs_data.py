"""Override validation, calibration regressions, and atomic profile refresh."""

import argparse
import os
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

import pytest
from familiar import cli
from familiar.interfaces import ConfigError, read_user_data, save_user_data, user_data_path
from familiar_scs.actions import action_mixes, action_specs, load_catalogue
from familiar_scs.adapter import AtsAdapter
from familiar_scs.bindings import BindingSource, default_bindings, parse_controls
from familiar_scs.commands import _bindings
from familiar_scs.places import load_regions
from familiar_scs.projection import PROJECTIONS, projection_for_game
from familiar_scs.telemetry import Snapshot

FIXTURE = Path(__file__).parent / "fixtures" / "controls_trimmed.sii"


@pytest.mark.parametrize(
    "override",
    [
        {"beacon": {"surprise": True}},
        {"beacon": {"description": 12}},
        {"beacon": {"control": []}},
        {"beacon": {"risky": "yes"}},
        {"beacon": {"toggle": 1}},
        {"beacon": {"hold_s": True}},
        {"beacon": {"hold_s": float("nan")}},
        {"beacon": {"hold_s": -1}},
        {"engine": {"state_field": "missing_field"}},
        {"engine": {"state_field": "speed"}},
        {"engine": {"state": {"extra": 1}}},
        {"engine": {"state": {"choices": [False, True]}}},
        {"engine": {"risky_when": {"state": "maybe"}}},
        {"blinker": {"variants": {"controls": []}}},
        {"blinker": {"variants": {"unknown": "mix"}}},
        {"beacon": "bad entry"},
    ],
)
def test_catalogue_validation_names_source(override):
    save_user_data("familiar_scs", "actions", override)
    with pytest.raises(ConfigError) as exc:
        load_catalogue()
    assert str(user_data_path("familiar_scs", "actions")) in str(exc.value)


def test_catalogue_override_adds_changes_and_deletes():
    save_user_data(
        "familiar_scs",
        "actions",
        {
            "beacon": {"description": "Custom beacon", "hold_s": 0.2},
            "custom": {"control": "invented", "description": "Custom action", "risky": True},
            "radio": None,
        },
    )
    catalogue = load_catalogue()
    specs = action_specs(catalogue)
    assert specs["beacon"].description == "Custom beacon"
    assert specs["custom"].risky
    assert action_mixes(catalogue)["custom"] == "invented"
    assert "radio" not in catalogue


def test_keys_and_regions_use_overrides():
    save_user_data("familiar_scs", "key_names", {"keys": {"space_laser": "f5"}})
    assert parse_controls("mix test `keyboard.space_laser?0`") == {"test": ("f5",)}
    save_user_data("familiar_scs", "default_keys", {"beacon": "leftshift+f5"})
    assert default_bindings()["beacon"] == ("leftshift", "f5")
    save_user_data("familiar_scs", "regions", {"ats": {"WY": "Custom Wyoming"}})
    assert load_regions()["ats"]["WY"] == "Custom Wyoming"


# Values captured from the original formula before extracting its parameters.
@pytest.mark.parametrize(
    "game,x,z,expected",
    [
        ("ats", 0, 0, (38.99999999999999, -96.0)),
        ("ats", 12345, -23456, (43.13645310334257, -93.00152507529234)),
        ("ats", -40000, -12000, (40.76850447207892, -105.39605477929703)),
        ("ets2", 0, 0, (50.63846416918776, 10.314502135768368)),
        ("ets2", 12345, -23456, (54.87412800507035, 13.664980265692312)),
        ("ets2", -40000, -12000, (51.46970746556445, -0.6183103776190481)),
        ("ets2", -32000, -8000, (51.31617478681526, 1.0358080973272625)),
    ],
)
def test_projection_calibration_unchanged(game, x, z, expected):
    # approx: libm results differ in the last bit between Linux and macOS.
    assert PROJECTIONS[game](x, z) == pytest.approx(expected, rel=1e-12)


def test_projection_override():
    original = projection_for_game("ats")(1000, 2000)
    save_user_data("familiar_scs", "projections", {"ats": {"offset_x": 1000}})
    assert projection_for_game("ats")(2000, 2000) == original


def write_profile(path, key, timestamp):
    path.write_text(f"mix beacon `keyboard.{key}?0`")
    os.utime(path, (timestamp, timestamp))


def test_binding_reload_swaps_whole_table_and_keeps_overrides(tmp_path):
    profile = tmp_path / "controls_linux.sii"
    write_profile(profile, "o", 10)
    source = BindingSource(
        "ats", {"profile": str(profile), "keys": {"horn": "f5"}}, action_mixes(load_catalogue())
    )
    old = source.table
    write_profile(profile, "b", 20)
    new = source.refresh()
    assert new is not old
    assert new == {"beacon": ("b",), "horn": ("f5",)}
    assert old["beacon"] == ("o",)
    assert source.refresh() is new


@pytest.mark.parametrize(
    "broken", ["garbage", "mix beacon `keyboard.b?0", "SiiNunit { mix beacon `keyboard.b?0`"]
)
def test_binding_reload_invalid_keeps_previous(tmp_path, caplog, broken):
    profile = tmp_path / "controls_linux.sii"
    write_profile(profile, "o", 10)
    source = BindingSource("ats", {"profile": str(profile)}, action_mixes(load_catalogue()))
    old = source.table
    profile.write_text(broken)
    assert source.refresh() is old
    assert "keeping previous table" in caplog.text
    assert str(profile) in caplog.text
    write_profile(profile, "b", 30)
    assert source.refresh()["beacon"] == ("b",)


def test_binding_reload_appears_and_vanishes(tmp_path, monkeypatch):
    monkeypatch.setattr("familiar_scs.bindings.sys.platform", "linux")
    source = BindingSource("ats", {"profile": str(tmp_path)}, action_mixes(load_catalogue()))
    assert source.table == default_bindings()
    path = tmp_path / "controls_linux.sii"
    write_profile(path, "b", 10)
    assert source.refresh() == {"beacon": ("b",)}
    path.unlink()
    assert source.refresh() == default_bindings()


async def test_adapter_refreshes_before_press_and_loads_custom_action(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr("familiar_scs.bindings.sys.platform", "linux")
    save_user_data(
        "familiar_scs",
        "actions",
        {"custom": {"control": "beacon", "description": "Custom", "hold_s": 0.25}},
    )
    profile = tmp_path / "controls_linux.sii"
    write_profile(profile, "o", 10)
    adapter = AtsAdapter({"profile": str(profile)})
    keyboard = SimpleNamespace(tap=AsyncMock(), aclose=AsyncMock())
    adapter.attach_keyboard(keyboard)
    adapter.latest = Snapshot(connected=True)
    adapter._watch_timestamp = monotonic()
    try:
        assert "custom" in adapter.actions()
        write_profile(profile, "b", 20)
        assert (await adapter.perform("custom", {}))["status"] == "done"
        keyboard.tap.assert_awaited_once_with(("b",), hold_s=0.25)
    finally:
        await adapter.aclose()


def binding_args(profile, suggest=False):
    return argparse.Namespace(game="ats", profile=str(profile), suggest=suggest)


def test_bindings_output(tmp_path, capsys):
    profile = tmp_path / "controls_linux.sii"
    profile.write_text(FIXTURE.read_text())
    _bindings(binding_args(profile), SimpleNamespace(games={}))
    output = capsys.readouterr().out
    assert f"Controls file: {profile}" in output
    assert "beacon: o" in output
    assert "radio: unbound (joystick-only?)" in output
    assert "blinker (left): unbound (joystick-only?)" in output
    unused = output.split("bound in your profile but not used by Familiar:")[1]
    assert "ctrl_test: leftctrl+2" in unused
    assert "prv_ctrl" not in unused
    assert "engine:" not in unused


def test_bindings_suggest_preserves_existing_entries(tmp_path, capsys):
    profile = tmp_path / "controls_linux.sii"
    profile.write_text(FIXTURE.read_text())
    existing = {"ctrl_test": None, "beacon": {"description": "Keep me"}}
    save_user_data("familiar_scs", "actions", existing)
    _bindings(binding_args(profile, suggest=True), SimpleNamespace(games={}))
    saved = read_user_data("familiar_scs", "actions")
    assert saved["ctrl_test"] is None
    assert saved["beacon"] == existing["beacon"]
    assert saved["alt_test"] == {
        "control": "alt_test",
        "description": "TODO: describe",
        "risky": True,
        "enabled": False,
    }
    assert "prv_ctrl" not in saved
    assert "#" not in user_data_path("familiar_scs", "actions").read_text()
    output = capsys.readouterr().out
    assert str(user_data_path("familiar_scs", "actions")) in output
    assert "Drafts are disabled" in output
    assert "alt_test" not in action_specs()  # drafts stay disabled until reviewed


def test_bindings_command_registration(monkeypatch):
    from familiar_scs import commands

    captured = []
    monkeypatch.setattr(commands, "_bindings", lambda args, config: captured.append(args))
    monkeypatch.setattr(
        "sys.argv", ["familiar", "scs", "bindings", "ets2", "--profile", "example", "--suggest"]
    )
    cli.main()
    assert captured[0].game == "ets2"
    assert captured[0].profile == "example"
    assert captured[0].suggest


def test_disabled_drafts_are_not_loaded_as_actions():
    from familiar.interfaces import save_user_data
    from familiar_scs.actions import action_specs, load_catalogue

    draft = {"control": "diflock", "description": "TODO: describe", "risky": True, "enabled": False}
    save_user_data("familiar_scs", "actions", {"diflock": draft})
    assert "diflock" not in load_catalogue()
    assert "diflock" not in action_specs()
    assert load_catalogue(include_disabled=True)["diflock"]["enabled"] is False
    save_user_data(
        "familiar_scs",
        "actions",
        {"diflock": {**draft, "description": "Toggle diff lock", "enabled": True}},
    )
    assert "diflock" in action_specs()
