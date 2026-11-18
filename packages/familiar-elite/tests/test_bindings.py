"""Fixture-based tests for the Elite .binds reader and Secondary writer."""

import hashlib
import json
import shutil
from argparse import ArgumentParser
from pathlib import Path

import pytest
from familiar.interfaces import ConfigError
from familiar_elite import bindings
from familiar_elite.bind_command import EliteBindings

FIXTURES = Path(__file__).parent / "fixtures"
EMPTY_SECONDARY_LINE = '\t\t<Secondary Device="{NoDevice}" Key="" />\r\n'

EXPECTED_PLAN = {
    "LandingGearToggle": "Key_A",
    "ToggleCargoScoop": "Key_B",
    "ResetPowerDistribution": "Key_C",
    "IncreaseSystemsPower": "Key_D",
    "CycleNextPanel": "Key_E",
    "CyclePreviousPanel": "Key_F",
}
EXPECTED_ALREADY_BOUND = {
    "ShipSpotLightToggle": "Key_L",
    "FocusLeftPanel": "Key_1",
    "UI_Select": "Key_Enter",
    "UI_Back": "Key_Escape",
    "UI_Up": "Key_UpArrow",
    "UI_Down": "Key_DownArrow",
    "UI_Left": "Key_LeftArrow",
    "UI_Right": "Key_RightArrow",
}


def _fixture_text() -> str:
    return (FIXTURES / "Custom.4.2.binds").read_bytes().decode("utf-8")


def _bindings_dir(tmp_path: Path) -> Path:
    """A Bindings folder with the fixture preset and its StartPreset file."""
    directory = tmp_path / "Bindings"
    directory.mkdir()
    for name in ("Custom.4.2.binds", "StartPreset.4.start"):
        shutil.copy(FIXTURES / name, directory / name)
    (directory / "StartPreset.4.start").write_text("Custom\n")
    return directory


def _presets(path: Path) -> dict[str, bindings.Preset]:
    return {
        category: bindings.Preset("Custom", path, True) for category in bindings.load_categories()
    }


def _plan() -> list[bindings.Assignment]:
    parsed = _presets(FIXTURES / "Custom.4.2.binds")
    return bindings.plan_secondaries(
        parsed, bindings.load_wanted_controls(), bindings.load_candidates()
    )


def _applied(tmp_path: Path) -> tuple[Path, bindings.ApplyResult]:
    preset = _bindings_dir(tmp_path) / "Custom.4.2.binds"
    result = bindings.apply_plan(
        _presets(preset),
        _plan(),
        backup_dir=tmp_path / "backups",
        sidecar_path=tmp_path / "owned_binds.json",
        proc_root=tmp_path / "proc",  # empty fake /proc: no game running
    )
    return preset, result


def _cli_args(command: EliteBindings, *arguments: str):
    parser = ArgumentParser(prog="familiar elite-bindings")
    command.add_arguments(parser)
    return parser.parse_args(arguments)


def _state_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(bindings, "default_backup_dir", lambda: tmp_path / "state" / "backups")
    monkeypatch.setattr(
        bindings, "default_sidecar_path", lambda: tmp_path / "state" / "owned_binds.json"
    )
    # The command applies through the real /proc scan; pretend the game is closed.
    monkeypatch.setattr(bindings, "game_running", lambda names, proc_root=None: None)
    return tmp_path / "state" / "owned_binds.json"


def test_active_preset_picks_highest_version(tmp_path):
    directory = _bindings_dir(tmp_path)
    (directory / "Custom.4.10.binds").write_bytes(b"<Root />")
    (directory / "Custom.3.9.binds").write_bytes(b"<Root />")
    assert bindings.active_presets(directory, None)["ship"].path == directory / "Custom.4.10.binds"


def test_active_preset_requires_start_file(tmp_path):
    with pytest.raises(ConfigError, match="StartPreset"):
        bindings.active_presets(tmp_path, None)


def test_active_preset_rejects_ambiguous(tmp_path):
    (tmp_path / "StartPreset.4.start").write_text("Alpha\nBeta\n")
    with pytest.raises(ConfigError, match="expected one preset"):
        bindings.active_presets(tmp_path, None)


def test_active_preset_requires_binds_file(tmp_path):
    (tmp_path / "StartPreset.4.start").write_text("Custom\n")
    with pytest.raises(ConfigError, match="Missing preset.*general"):
        bindings.active_presets(tmp_path, None)


def test_parse_binds_reads_slots_and_skips_axes():
    parsed = bindings.parse_binds(_fixture_text())
    assert "YawAxisRaw" not in parsed  # axis controls carry Binding, not Primary/Secondary
    assert parsed["LandingGearToggle"].primary == bindings.Slot("HOTASOne", "Joy_5")
    assert parsed["LandingGearToggle"].secondary == bindings.Slot("{NoDevice}", "")
    assert parsed["NightVisionToggle"].secondary == bindings.Slot("HOTASOne", "Joy_7")
    assert parsed["FocusLeftPanel"].primary.modifiers == (("Keyboard", "Key_LeftShift"),)


def test_keyboard_keys_in_use_includes_modifiers():
    keys = bindings.keyboard_keys_in_use(bindings.parse_binds(_fixture_text()))
    assert keys == {
        "Key_L",
        "Key_1",
        "Key_LeftShift",
        "Key_Enter",
        "Key_Escape",
        "Key_UpArrow",
        "Key_DownArrow",
        "Key_LeftArrow",
        "Key_RightArrow",
        "Key_V",
    }


def test_plan_secondaries_assigns_free_keys_without_duplicates():
    plan = {entry.control: entry for entry in _plan()}
    planned = {c: a.key for c, a in plan.items() if a.status == bindings.PLANNED}
    assert planned == EXPECTED_PLAN
    assert len(set(planned.values())) == len(planned)  # never the same key twice
    bound = {c: a.key for c, a in plan.items() if a.status == bindings.ALREADY_BOUND}
    assert bound == EXPECTED_ALREADY_BOUND
    assert plan["NightVisionToggle"].status == bindings.SECONDARY_OCCUPIED
    assert plan["NightVisionToggle"].detail == "HOTASOne"


def test_plan_secondaries_reports_missing_controls():
    parsed = _presets(FIXTURES / "Custom.4.2.binds")
    plan = bindings.plan_secondaries(
        parsed, {"NoSuchControl": {"category": "ship", "purpose": "?"}}, ["Key_A"]
    )
    assert plan == [bindings.Assignment("NoSuchControl", "", bindings.MISSING)]


def test_apply_plan_rewrites_only_empty_secondary_lines(tmp_path):
    preset, result = _applied(tmp_path)
    assert result.assigned == EXPECTED_PLAN
    before = _fixture_text().splitlines(keepends=True)
    after = preset.read_bytes().decode("utf-8").splitlines(keepends=True)
    changed = [(old, new) for old, new in zip(before, after, strict=True) if old != new]
    assert len(changed) == len(EXPECTED_PLAN)
    for old, new in changed:
        assert old == EMPTY_SECONDARY_LINE
        assert new.startswith('\t\t<Secondary Device="Keyboard" Key="')
        assert new.endswith('" />\r\n')
    assert {new.split('Key="')[1].split('"')[0] for _, new in changed} == set(
        EXPECTED_PLAN.values()
    )
    reparsed = bindings.parse_binds(preset.read_bytes().decode("utf-8"))
    for control, key in EXPECTED_PLAN.items():
        assert reparsed[control].secondary == bindings.Slot("Keyboard", key)
    assert reparsed["ShipSpotLightToggle"].secondary == bindings.Slot("Keyboard", "Key_L")


def test_apply_plan_writes_backup_and_sidecar(tmp_path):
    original = (FIXTURES / "Custom.4.2.binds").read_bytes()
    preset, result = _applied(tmp_path)
    backups = list((tmp_path / "backups").glob("Custom.4.2.*.binds"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    sidecar = json.loads((tmp_path / "owned_binds.json").read_text(encoding="utf-8"))
    assert set(sidecar) == {str(preset)}
    assert sidecar[str(preset)]["sha256"] == hashlib.sha256(preset.read_bytes()).hexdigest()
    assert sidecar[str(preset)]["owned"] == EXPECTED_PLAN
    assert result.backups == (backups[0],)
    assert result.sidecar == tmp_path / "owned_binds.json"


def test_apply_plan_is_idempotent_and_leaves_existing_keyboard_bindings(tmp_path):
    preset, first = _applied(tmp_path)
    assert first.assigned == EXPECTED_PLAN
    plan = bindings.plan_secondaries(
        _presets(preset), bindings.load_wanted_controls(), bindings.load_candidates()
    )
    assert not [entry for entry in plan if entry.status == bindings.PLANNED]
    written = preset.read_bytes()
    second = bindings.apply_plan(
        _presets(preset),
        plan,
        backup_dir=tmp_path / "backups",
        sidecar_path=tmp_path / "owned_binds.json",
        proc_root=tmp_path / "proc",
    )
    assert second.assigned == {}
    assert preset.read_bytes() == written


def test_apply_plan_refuses_while_game_runs(tmp_path, monkeypatch):
    preset = _bindings_dir(tmp_path) / "Custom.4.2.binds"
    original = preset.read_bytes()
    monkeypatch.setattr(
        bindings, "game_running", lambda names, proc_root=None: "elitedangerous64.exe"
    )
    with pytest.raises(ConfigError, match="running"):
        bindings.apply_plan(
            _presets(preset),
            _plan(),
            backup_dir=tmp_path / "backups",
            sidecar_path=tmp_path / "sidecar",
        )
    assert preset.read_bytes() == original
    assert not (tmp_path / "backups").exists()


def test_apply_plan_preserves_lf_endings(tmp_path):
    preset = _bindings_dir(tmp_path) / "Custom.4.2.binds"
    preset.write_bytes(_fixture_text().replace("\r\n", "\n").encode("utf-8"))
    bindings.apply_plan(
        _presets(preset),
        _plan(),
        backup_dir=tmp_path / "backups",
        sidecar_path=tmp_path / "sidecar",
        proc_root=tmp_path / "proc",
    )
    assert b"\r" not in preset.read_bytes()


def test_game_running_scans_proc(tmp_path):
    proc = tmp_path / "proc"
    (proc / "4321").mkdir(parents=True)
    (proc / "4321" / "cmdline").write_bytes(b"C:\\games\\EliteDangerous64.exe\x00-AO\x00")
    (proc / "not-a-pid").mkdir()
    assert bindings.game_running(["EliteDangerous64.exe"], proc) == "elitedangerous64.exe"
    assert bindings.game_running(["notrunning.exe"], proc) is None


def test_check_sidecar_clean_after_apply(tmp_path):
    preset, _ = _applied(tmp_path)
    assert bindings.check_sidecar(_presets(preset), tmp_path / "owned_binds.json") == []


def test_check_sidecar_missing_sidecar_is_quiet(tmp_path):
    preset = _bindings_dir(tmp_path) / "Custom.4.2.binds"
    assert bindings.check_sidecar(_presets(preset), tmp_path / "nope.json") == []


def test_check_sidecar_detects_edits(tmp_path):
    preset, _ = _applied(tmp_path)
    edited = preset.read_bytes().decode("utf-8").replace('Key="Key_A"', 'Key="Key_Q"', 1)
    preset.write_bytes(edited.encode("utf-8"))
    warnings = bindings.check_sidecar(_presets(preset), tmp_path / "owned_binds.json")
    assert any("changed since Familiar wrote it" in warning for warning in warnings)
    assert any("LandingGearToggle" in warning and "Key_A" in warning for warning in warnings)


def test_keyboard_map_translates_chords_modifiers_first():
    mapping = bindings.keyboard_map(
        bindings.parse_binds(_fixture_text()), bindings.load_key_names()
    )
    assert mapping["UI_Select"] == ("enter",)
    assert mapping["UI_Back"] == ("esc",)
    assert mapping["UI_Up"] == ("up",)
    assert mapping["ShipSpotLightToggle"] == ("l",)  # Secondary when Primary is on the HOTAS
    assert mapping["FocusLeftPanel"] == ("leftshift", "1")
    assert "LandingGearToggle" not in mapping  # joystick-only control


def test_keyboard_map_skips_unknown_names(caplog):
    text = _fixture_text().replace('Key="Key_V"', 'Key="Key_Mystery"')
    with caplog.at_level("DEBUG", logger="familiar_elite.bindings"):
        mapping = bindings.keyboard_map(bindings.parse_binds(text), bindings.load_key_names())
    assert "DeployHeatSink" not in mapping
    assert "Key_Mystery" in caplog.text


def test_command_dry_run_prints_plan_without_writing(tmp_path, capsys):
    directory = _bindings_dir(tmp_path)
    preset = directory / "Custom.4.2.binds"
    original = preset.read_bytes()
    command = EliteBindings()
    code = command.run(_cli_args(command, "--bindings-dir", str(directory)), None)
    out = capsys.readouterr().out
    assert code == 0
    assert "Category" in out
    assert "ship" in out
    assert "Custom" in out
    assert "LandingGearToggle: Key_A (will bind)" in out
    assert "ShipSpotLightToggle: Key_L (already bound)" in out
    assert "NightVisionToggle: secondary occupied by HOTASOne; skipped" in out
    assert "Dry run: 6 Secondary key(s)" in out
    assert preset.read_bytes() == original


def test_command_write_applies_and_mentions_restart(tmp_path, capsys, monkeypatch):
    sidecar = _state_dirs(monkeypatch, tmp_path)
    directory = _bindings_dir(tmp_path)
    command = EliteBindings()
    code = command.run(_cli_args(command, "--bindings-dir", str(directory), "--write"), None)
    out = capsys.readouterr().out
    assert code == 0
    assert "Wrote 6 Secondary key(s)" in out
    assert "Restart Elite to load the new keys." in out
    assert sidecar.exists()


def test_command_check_reports_clean_then_drift(tmp_path, capsys, monkeypatch):
    _state_dirs(monkeypatch, tmp_path)
    directory = _bindings_dir(tmp_path)
    preset = directory / "Custom.4.2.binds"
    command = EliteBindings()
    args = ["--bindings-dir", str(directory)]
    assert command.run(_cli_args(command, *args, "--write"), None) == 0
    capsys.readouterr()
    assert command.run(_cli_args(command, *args, "--check"), None) == 0
    assert "owned keys intact" in capsys.readouterr().out
    edited = preset.read_bytes().decode("utf-8").replace('Key="Key_A"', 'Key="Key_Q"', 1)
    preset.write_bytes(edited.encode("utf-8"))
    assert command.run(_cli_args(command, *args, "--check"), None) == 1
    assert "warning:" in capsys.readouterr().out


def test_game_running_ignores_mentions_in_arguments(tmp_path):
    proc = tmp_path / "proc"
    (proc / "77").mkdir(parents=True)
    (proc / "77" / "cmdline").write_bytes(b"copilot\x00-p\x00close EliteDangerous64.exe first\x00")
    assert bindings.game_running(["EliteDangerous64.exe"], proc) is None


def test_chord_with_unknown_modifier_is_skipped():
    slot = bindings.Slot("Keyboard", "Key_K", (("Keyboard", "Key_Mystery"),))
    binding = bindings.Binding(slot, bindings.Slot(bindings.NO_DEVICE, ""))
    assert bindings.keyboard_map({"Thing": binding}, {"Key_K": "k"}) == {}


@pytest.fixture
def category_presets(tmp_path):
    directory = _bindings_dir(tmp_path)
    builtin = tmp_path / "ControlSchemes"
    builtin.mkdir()
    text = _fixture_text().replace('Key="Key_UpArrow"', 'Key="Key_W"')
    text = text.replace('Key="Key_V"', 'Key="Key_A"')
    (builtin / "TFlightHotasOne.binds").write_bytes(text.encode("utf-8"))
    (directory / "StartPreset.4.start").write_text(
        "TFlightHotasOne\nCustom\nTFlightHotasOne\nTFlightHotasOne\n"
    )
    return directory, builtin, bindings.active_presets(directory, builtin)


def test_category_presets_resolve_user_and_builtin(category_presets):
    directory, builtin, presets = category_presets
    assert presets["ship"] == bindings.Preset("Custom", directory / "Custom.4.2.binds", True)
    assert presets["general"] == bindings.Preset(
        "TFlightHotasOne", builtin / "TFlightHotasOne.binds", False
    )
    assert presets["srv"] == presets["on_foot"] == presets["general"]
    (directory / "TFlightHotasOne.4.3.binds").write_text("<Root />")
    assert bindings.active_presets(directory, builtin)["general"].writable


def test_one_line_start_applies_to_every_category(tmp_path):
    directory = _bindings_dir(tmp_path)
    presets = bindings.active_presets(directory, None)
    assert len(presets) == 4
    assert set(presets.values()) == {
        bindings.Preset("Custom", directory / "Custom.4.2.binds", True)
    }


def test_missing_preset_names_category(category_presets):
    directory, builtin, _ = category_presets
    (directory / "StartPreset.4.start").write_text(
        "TFlightHotasOne\nCustom\nGone\nTFlightHotasOne\n"
    )
    with pytest.raises(ConfigError, match="Gone.*category srv"):
        bindings.active_presets(directory, builtin)


def test_builtin_advice_and_global_key_conflicts(category_presets):
    _, _, presets = category_presets
    plan = bindings.plan_secondaries(
        presets, bindings.load_wanted_controls(), ["Key_A", "Key_W", "Key_B", "Key_C"]
    )
    table = {entry.control: entry for entry in plan}
    assert table["UI_Up"].status == bindings.ALREADY_BOUND
    assert table["UI_Up"].key == "Key_W"
    assert table["CycleNextPanel"].status == bindings.BUILTIN_PRESET
    assert table["CycleNextPanel"].detail == (
        "set General controls to Custom in Options > Controls, then rerun"
    )
    assert table["LandingGearToggle"].key == "Key_B"
    assert all(
        entry.key not in {"Key_A", "Key_W"} for entry in plan if entry.status == bindings.PLANNED
    )


def test_apply_only_writable_file(category_presets, tmp_path):
    _, builtin, presets = category_presets
    builtin_path = builtin / "TFlightHotasOne.binds"
    original = builtin_path.read_bytes()
    plan = bindings.plan_secondaries(
        presets, bindings.load_wanted_controls(), bindings.load_candidates()
    )
    # Even a manually constructed assignment cannot write a built-in preset.
    plan.append(bindings.Assignment("CycleNextPanel", "Key_Z", bindings.PLANNED))
    sidecar = tmp_path / "sidecar.json"
    result = bindings.apply_plan(
        presets,
        plan,
        backup_dir=tmp_path / "backups",
        sidecar_path=sidecar,
        proc_root=tmp_path / "proc",
    )
    assert len(result.backups) == 1
    assert builtin_path.read_bytes() == original
    assert set(json.loads(sidecar.read_text())) == {str(presets["ship"].path)}
    assert "CycleNextPanel" not in result.assigned
    assert bindings.check_sidecar(presets, sidecar) == []


def test_multi_file_sidecar_and_backups(category_presets, tmp_path):
    directory, _, presets = category_presets
    general_path = directory / "General.4.2.binds"
    general_path.write_bytes(_fixture_text().encode("utf-8"))
    presets["general"] = bindings.Preset("General", general_path, True)
    sidecar = tmp_path / "sidecar.json"
    plan = bindings.plan_secondaries(
        presets, bindings.load_wanted_controls(), bindings.load_candidates()
    )
    result = bindings.apply_plan(
        presets,
        plan,
        backup_dir=tmp_path / "backups",
        sidecar_path=sidecar,
        proc_root=tmp_path / "proc",
    )
    assert len(result.backups) == 2
    payload = json.loads(sidecar.read_text())
    assert set(payload) == {str(general_path), str(presets["ship"].path)}
    assert payload[str(general_path)]["owned"] == {
        "CycleNextPanel": "Key_F",
        "CyclePreviousPanel": "Key_G",
    }
    assert bindings.check_sidecar(presets, sidecar) == []
    for path in (general_path, presets["ship"].path):
        path.write_bytes(path.read_bytes().replace(b'Device="Keyboard"', b'Device="Gone"'))
    warnings = bindings.check_sidecar(presets, sidecar)
    assert sum("changed since" in warning for warning in warnings) == 2
    assert any("CycleNextPanel" in warning for warning in warnings)
    assert any("LandingGearToggle" in warning for warning in warnings)


def test_active_keyboard_map_follows_categories(category_presets):
    directory, builtin, _ = category_presets
    mapping = bindings.active_keyboard_map(directory, builtin)
    assert mapping["UI_Up"] == ("w",)
    assert mapping["ShipSpotLightToggle"] == ("l",)
    assert mapping["DeployHeatSink"] == ("v",)  # unknown control prefers ship
    assert "CycleNextPanel" not in mapping
    ship = directory / "Custom.4.2.binds"
    text = ship.read_bytes().decode("utf-8")
    text = text.replace('Key="Key_V"', 'Key=""')
    text = text.replace('Key="Key_L"', 'Key=""')
    ship.write_bytes(text.encode("utf-8"))
    mapping = bindings.active_keyboard_map(directory, builtin)
    assert mapping["DeployHeatSink"] == ("a",)  # fallback to first preset binding it
    assert "ShipSpotLightToggle" not in mapping  # known category never falls through


def test_command_reports_category_table_and_builtin_advice(category_presets, capsys):
    directory, builtin, _ = category_presets
    command = EliteBindings()
    code = command.run(
        _cli_args(command, "--bindings-dir", str(directory), "--control-schemes-dir", str(builtin)),
        None,
    )
    output = capsys.readouterr().out
    assert code == 0
    assert "TFlightHotasOne" in output
    assert "False" in output
    assert "UI_Up: Key_W (already bound)" in output
    assert "set General controls to Custom in Options > Controls, then rerun" in output


def test_command_write_keeps_builtin_advice(category_presets, tmp_path, monkeypatch, capsys):
    directory, builtin, _ = category_presets
    _state_dirs(monkeypatch, tmp_path)
    command = EliteBindings()
    assert (
        command.run(
            _cli_args(
                command,
                "--bindings-dir",
                str(directory),
                "--control-schemes-dir",
                str(builtin),
                "--write",
            ),
            None,
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Wrote 4 Secondary key(s)" in output
    assert "set General controls to Custom in Options > Controls, then rerun" in output


def test_invalid_sidecar_refuses_before_writing(category_presets, tmp_path):
    _, _, presets = category_presets
    path = presets["ship"].path
    original = path.read_bytes()
    sidecar = tmp_path / "sidecar.json"
    sidecar.write_text("broken json")
    plan = bindings.plan_secondaries(
        presets, bindings.load_wanted_controls(), bindings.load_candidates()
    )
    with pytest.raises(ConfigError, match="sidecar"):
        bindings.apply_plan(
            presets,
            plan,
            backup_dir=tmp_path / "backups",
            sidecar_path=sidecar,
            proc_root=tmp_path / "proc",
        )
    assert path.read_bytes() == original
    assert not (tmp_path / "backups").exists()
