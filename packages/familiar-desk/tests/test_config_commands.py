"""Desk settings, shared core registration, and CLI behavior."""

import argparse
import asyncio
import signal
from contextlib import AsyncExitStack
from unittest.mock import AsyncMock, Mock

import pytest
from familiar.cli import _Runtime
from familiar.config import AppConfig, load_config
from familiar.interfaces import ConfigError
from familiar_desk.commands import DeskFeedCommand
from familiar_desk.feed import Feed
from familiar_desk.paths import feed_path


def test_config_defaults_and_path(tmp_path):
    path = tmp_path / "desk.toml"
    path.write_text(
        "[desk]\nenabled = true\nlatitude = 19.4\n"
        'longitude = -99.1\ntimezone = "America/Mexico_City"\n'
    )
    config = load_config(path)
    assert config.desk["enabled"]
    assert config.config_path == path.resolve()
    assert config.desk["interval_s"] == 60
    assert not AppConfig().desk["enabled"]


@pytest.mark.parametrize(
    "section",
    [
        "desk = false",
        "[desk]\nenabled = 1",
        "[desk]\ninterval_s = 0",
        "[desk]\ninterval_s = inf",
        "[desk]\nlatitude = 91\nlongitude = 0",
        "[desk]\nlatitude = 10",
        "[desk]\nlatitude = true\nlongitude = 0",
        '[desk]\ntimezone = "invalid/zone"',
        "[desk]\ntimezone = 12",
        '[desk]\nlocation = " "',
        "[desk]\nlocation = 5",
    ],
)
def test_invalid_config(tmp_path, section):
    path = tmp_path / "desk.toml"
    path.write_text(section)
    with pytest.raises(ConfigError, match="desk"):
        load_config(path)


@pytest.mark.asyncio
async def test_shared_service_registration(monkeypatch):
    tool = Mock(carrier_only=True, autonomous=True)
    service = Mock(service_tools=Mock(return_value=[tool]), aclose=AsyncMock())
    load = Mock(return_value=service)
    monkeypatch.setattr("familiar.cli.load", load)
    async with AsyncExitStack() as stack:
        runtime = _Runtime(AppConfig(), Mock(), "en", stack)
        conversation = Mock()
        runtime._service(conversation, "desk", {"interval_s": 60})
        load.assert_called_once_with("services", "desk", {"interval_s": 60})
        conversation.tools.register.assert_called_once_with(
            tool.spec, tool.handler, carrier_only=True, autonomous=True
        )
        assert runtime.services == [service]
    service.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_service_load_failure_isolated(monkeypatch):
    monkeypatch.setattr("familiar.cli.load", Mock(side_effect=RuntimeError("broken")))
    async with AsyncExitStack() as stack:
        runtime = _Runtime(AppConfig(), Mock(), "en", stack)
        runtime._service(Mock(), "desk", {})
        assert runtime.services == []


@pytest.mark.asyncio
async def test_command_once_and_lock_conflict(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    command = DeskFeedCommand()
    parser = argparse.ArgumentParser()
    command.add_arguments(parser)
    args = parser.parse_args(["--once", "--interval", "2"])
    assert await command.run(args, AppConfig()) == 0
    assert feed_path().exists()
    owner = Feed([])
    owner.acquire()
    try:
        assert await command.run(args, AppConfig()) == 1
    finally:
        owner.close()
    assert await command.run(parser.parse_args(["--interval", "nan"]), AppConfig()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
async def test_command_signal_releases_lock(tmp_path, monkeypatch, sig):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    loop = asyncio.get_running_loop()
    handlers = {}
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda sig, callback: handlers.update({sig: callback})
    )
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: handlers.pop(sig))
    original = Feed.sample

    async def sample(self, source):
        await original(self, source)
        handlers[sig]()

    monkeypatch.setattr(Feed, "sample", sample)
    args = argparse.Namespace(once=False, interval=None)
    assert await DeskFeedCommand().run(args, AppConfig()) == 0
    assert handlers == {}
    assert feed_path().exists()
    owner = Feed([])
    owner.acquire()
    owner.close()
