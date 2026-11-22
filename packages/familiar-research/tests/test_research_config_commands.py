"""Core integration and permission check records."""

import argparse
import asyncio
import json
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from familiar.cli import _Runtime
from familiar.config import AppConfig, load_config
from familiar.director import Director
from familiar.interfaces import ConfigError
from familiar.interfaces.types import GameEvent
from familiar_research.agy import AgyRunner, profile_hash, settings_for
from familiar_research.commands import ResearchCommand, check_path


def test_config_defaults_and_overrides(tmp_path):
    assert AppConfig().research == {"enabled": False}
    path = tmp_path / "config.toml"
    path.write_text('[research]\nenabled = true\nmodel = "override"\ntimeout_s = 42.5\n')
    assert load_config(path).research == {"enabled": True, "model": "override", "timeout_s": 42.5}


@pytest.mark.parametrize(
    "section",
    [
        "research = false",
        "[research]\nenabled = 1",
        '[research]\nmodel = " "',
        "[research]\nmodel = 3",
        "[research]\ntimeout_s = 0",
        "[research]\ntimeout_s = -1",
        "[research]\ntimeout_s = inf",
        "[research]\ntimeout_s = nan",
        "[research]\ntimeout_s = true",
        '[research]\ntimeout_s = "90"',
    ],
)
def test_invalid_config(tmp_path, section):
    path = tmp_path / "config.toml"
    path.write_text(section)
    with pytest.raises(ConfigError, match="research"):
        load_config(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("game", [None, "test-game"])
async def test_research_in_desk_and_game_sessions(monkeypatch, game):
    adapter = Mock(spec=["game", "describe", "aclose"], game="test-game", aclose=AsyncMock())
    service = Mock(service_tools=Mock(return_value=[]), aclose=AsyncMock())
    plugins = {("games", "test-game"): adapter, ("services", "research"): service}
    load = Mock(side_effect=lambda kind, name, settings: plugins[kind, name])
    monkeypatch.setattr("familiar.cli.load", load)
    conversation = Mock()
    conversation.crew.members = []
    monkeypatch.setattr("familiar.cli.Conversation", Mock(return_value=conversation))
    config = AppConfig(
        game=game, research={"enabled": True}, memory={"enabled": False}, flavor={"enabled": False}
    )
    async with AsyncExitStack() as stack:
        runtime = _Runtime(config, Mock(chattiness=0.5), "en", stack)
        monkeypatch.setattr(runtime, "backend", Mock())
        runtime.conversation(argparse.Namespace(game=None))
        assert runtime.services == [service]
    service.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_disabled_research_is_not_loaded():
    async with AsyncExitStack() as stack:
        runtime = _Runtime(AppConfig(), Mock(), "en", stack)
        runtime._service = Mock()
        runtime._research(Mock())
        runtime._service.assert_not_called()


def test_distinct_research_results_remain_queued():
    director = Director(0.5, {})
    events = [
        GameEvent(
            kind="research_result",
            priority="notable",
            timestamp=datetime.now(UTC),
            data={"research_id": key, "cue": True, "companion_service": True, "summary": "Answer"},
        )
        for key in ("one", "two")
    ]
    for event in events:
        director.submit(event)
    assert len(director._queue) == 2
    assert director._take() == events[0]
    assert director._take() == events[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "control, leak, passed",
    [
        (("marker.txt", "shellmark"), (), True),  # tries both; the profile blocks both
        (("marker.txt", "shellmark"), ("marker.txt",), False),  # a write gets through
        (("marker.txt", "shellmark"), ("shellmark",), False),  # a command gets through
        ((), (), False),  # never tries: the check proves nothing, so it fails
    ],
)
async def test_check_pass_fail_with_real_profile(
    tmp_path, monkeypatch, capsys, control, leak, passed
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    calls = []

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        if "-p" in args:
            unrestricted = not json.loads(args[-1])["permissions"]["deny"]
            for name in control if unrestricted else leak:
                (Path(kwargs["cwd"]) / name).touch()
        output = b"agy test\n" if "--version" in args else b"irrelevant free text"
        return Mock(returncode=0, communicate=AsyncMock(return_value=(output, None)))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    command = ResearchCommand()
    parser = argparse.ArgumentParser()
    command.add_arguments(parser)
    status = await command.run(parser.parse_args(["check"]), AppConfig())
    assert status == (0 if passed else 1)
    output = capsys.readouterr().out
    expected = "PASS" if passed else "FAIL"
    assert output == f"vault: {expected}\nweb: {expected}\n"
    assert len({kwargs["cwd"] for _, kwargs in calls}) == len(calls)
    assert all(not Path(kwargs["cwd"]).exists() for _, kwargs in calls)
    if not passed:
        assert not check_path().exists()
        return
    assert len(calls) == 5  # version, then control and restricted run per scope
    control_args, vault_args = calls[1][0], calls[2][0]
    assert json.loads(control_args[-1])["permissions"]["deny"] == []
    assert "--sandbox" in vault_args
    assert "marker.txt" in vault_args[2] and "touch shellmark" in vault_args[2]
    assert json.loads(vault_args[-1])["permissions"]["deny"] == settings_for()["deny"] + [
        "read_url(*)"
    ]
    record = json.loads(check_path().read_text())
    assert record["agy_version"] == "agy test"
    assert record["profile_hash"] == profile_hash(settings_for())
    assert datetime.fromisoformat(record["passed_at"]).tzinfo is not None


@pytest.mark.asyncio
async def test_failed_check_invalidates_previous_record(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    check_path().parent.mkdir(parents=True)
    check_path().write_text("old record")
    monkeypatch.setattr(AgyRunner, "version", AsyncMock(return_value="version"))
    monkeypatch.setattr(AgyRunner, "check", AsyncMock(return_value=False))
    assert await ResearchCommand().run(argparse.Namespace(action="check"), AppConfig()) == 1
    assert not check_path().exists()


def test_permission_hash_changes_with_profiles():
    original = settings_for()
    changed = settings_for()
    changed["scopes"]["vault"]["allow"].append("mcp(new/tool)")
    assert profile_hash(original) != profile_hash(changed)
