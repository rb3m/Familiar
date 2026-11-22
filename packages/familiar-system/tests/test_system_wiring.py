import argparse
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from familiar import cli
from familiar.config import AppConfig, load_config
from familiar.interfaces import ConfigError
from familiar_system.commands import SystemCommand
from familiar_system.readers import Temperature


@pytest.mark.parametrize(
    "section",
    [
        'system = "bad"',
        "[system]\nenabled = 1",
        "[system]\nthermal_interval_s = 0",
        "[system]\nstorage_interval_s = -1",
        "[system]\nthermal_interval_s = inf",
        "[system]\nstorage_interval_s = nan",
        "[system]\nthermal_interval_s = true",
    ],
)
def test_config_validation(tmp_path, section):
    path = tmp_path / "settings.toml"
    path.write_text(section)
    with pytest.raises(ConfigError, match="system"):
        load_config(path)


def test_config_defaults_and_overrides(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text("")
    assert load_config(path).system == {"enabled": False}
    path.write_text("[system]\nenabled = true\nthermal_interval_s = 2.5\nstorage_interval_s = 30")
    assert load_config(path).system == {
        "enabled": True,
        "thermal_interval_s": 2.5,
        "storage_interval_s": 30,
    }


@pytest.mark.parametrize("game", [None, "mockgame"])
@pytest.mark.parametrize("enabled", [True, False])
def test_runtime_wires_system_for_desk_and_game(monkeypatch, game, enabled):
    config = AppConfig(
        game=game,
        system={"enabled": enabled},
        memory={"enabled": False},
        alerts={"enabled": False},
        hands={"enabled": False},
        flavor={"enabled": False},
    )
    adapter = SimpleNamespace(game=game, describe=lambda: "")
    conversation = SimpleNamespace(
        crew=SimpleNamespace(members=[]), memory=None, banter_blocked=lambda: False
    )
    monkeypatch.setattr(cli, "Conversation", lambda *args, **kwargs: conversation)
    monkeypatch.setattr(cli, "TranscriptLog", Mock())
    monkeypatch.setattr(cli, "load", lambda *args: adapter)
    monkeypatch.setattr(
        cli, "Director", lambda *args, **kwargs: SimpleNamespace(banter_started=None)
    )
    monkeypatch.setattr(cli._Runtime, "backend", Mock())
    runtime = cli._Runtime(config, SimpleNamespace(chattiness=0.5), "en", Mock())
    register = Mock()
    monkeypatch.setattr(runtime, "_service", register)
    runtime.conversation(argparse.Namespace(game=None))
    expected = [("system", {"enabled": True})] * int(enabled)
    assert [(call.args[1], call.args[2]) for call in register.call_args_list] == expected


def test_inventory_statuses(capsys, settings):
    SystemCommand._sensor(Temperature(driver="other", label="board", celsius=50), settings)
    SystemCommand._sensor(Temperature(driver="nvme", label="Composite", celsius=127), settings)
    SystemCommand._sensor(Temperature(driver="nvme", label="Composite", celsius=65), settings)
    SystemCommand._partition(SimpleNamespace(mountpoint="/net", fstype="cifs"), set(), settings)
    SystemCommand._partition(
        SimpleNamespace(mountpoint="/mock", fstype="ext4"), {"/mock"}, settings
    )
    output = capsys.readouterr().out
    assert "ignored (no rule)" in output
    assert "ignored (implausible)" in output
    assert "reported as " in output
    assert "skipped network" in output
    assert "included" in output


@pytest.mark.parametrize("game", [False, True])
async def test_text_chat_starts_service_event_feed(monkeypatch, game):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    service = object()
    adapter = None
    if game:
        adapter = object()
    director = SimpleNamespace(next=AsyncMock(side_effect=asyncio.CancelledError))
    conversation = SimpleNamespace(memory=None, alerts=None)
    runtime = SimpleNamespace(
        services=[service],
        persona=SimpleNamespace(display_name="Mock Mira"),
        conversation=lambda args: (conversation, adapter, director),
    )

    @asynccontextmanager
    async def runtime_context(args):
        yield runtime

    feed = AsyncMock()
    monkeypatch.setattr(cli, "_runtime", runtime_context)
    monkeypatch.setattr(cli, "_feed_service", feed)
    game_feed = AsyncMock()
    monkeypatch.setattr(cli, "feed_events", game_feed)
    monkeypatch.setattr("builtins.input", lambda *args: "/quit")
    await cli._chat(argparse.Namespace())
    feed.assert_any_call(service, director)
    if game:
        game_feed.assert_any_call(adapter, director, None)
