"""Entry point discovery and plugin API compatibility."""

from dataclasses import dataclass

import pytest
from familiar import plugins
from familiar.interfaces import PluginError


@dataclass
class FakeEntryPoint:
    name: str
    plugin: type

    def load(self):
        return self.plugin


class FakePlugin:
    api_version = "2.7"

    def __init__(self, settings):
        self.settings = settings


def test_gemini_registered():
    assert "gemini" in plugins.available("llm")


def test_unknown_kind():
    with pytest.raises(PluginError, match="llm.*stt.*tts.*games"):
        plugins.available("missing")


def test_unknown_name(monkeypatch):
    monkeypatch.setattr(plugins, "entry_points", lambda **_: [FakeEntryPoint("fake", FakePlugin)])
    with pytest.raises(PluginError, match="Available names: fake"):
        plugins.load("llm", "missing", {})


def test_compatible_and_incompatible(monkeypatch):
    monkeypatch.setattr(plugins, "entry_points", lambda **_: [FakeEntryPoint("fake", FakePlugin)])
    assert plugins.load("llm", "fake", {"test": 1}).settings == {"test": 1}
    monkeypatch.setattr(FakePlugin, "api_version", "1.0")
    with pytest.raises(PluginError, match="incompatible API version"):
        plugins.load("llm", "fake", {})


@pytest.mark.parametrize("failure", ["load", "version", "arguments"])
def test_broken_command_cannot_disable_cli(monkeypatch, capsys, failure):
    from familiar import cli
    from familiar.interfaces import PLUGIN_API_VERSION

    class Command:
        name = "broken"
        help = "Broken command"
        api_version = "999.0" if failure == "version" else PLUGIN_API_VERSION

        def add_arguments(self, parser):
            raise RuntimeError("broken arguments")

    class Entry(FakeEntryPoint):
        def load(self):
            if failure == "load":
                raise RuntimeError("broken import")
            return super().load()

    monkeypatch.setattr(
        plugins,
        "entry_points",
        lambda **kwargs: (
            [Entry("broken", Command)] if kwargs["group"] == "familiar.commands" else []
        ),
    )
    monkeypatch.setattr("sys.argv", ["familiar", "plugins"])
    cli.main()
    captured = capsys.readouterr()
    assert "games: (none)" in captured.out
    assert "Warning: skipping command 'broken'" in captured.err


@pytest.mark.parametrize("asynchronous", [False, True])
def test_command_receives_config_and_returns_exit_status(monkeypatch, tmp_path, asynchronous):
    from familiar import cli
    from familiar.interfaces import PLUGIN_API_VERSION

    config_path = tmp_path / "config.toml"
    config_path.write_text('language = "ja"\n')
    captured = []

    class Command:
        name = "example"
        help = "Example command"
        api_version = PLUGIN_API_VERSION

        def add_arguments(self, parser):
            parser.add_argument("value")

        def run(self, args, config):
            captured.append((args.value, config.language))
            return 7

    if asynchronous:
        run = Command.run

        async def async_run(self, args, config):
            return run(self, args, config)

        Command.run = async_run
    monkeypatch.setattr(
        plugins,
        "entry_points",
        lambda **kwargs: (
            [FakeEntryPoint("example", Command)] if kwargs["group"] == "familiar.commands" else []
        ),
    )
    monkeypatch.setattr("sys.argv", ["familiar", "example", "hello", "--config", str(config_path)])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 7
    assert captured == [("hello", "ja")]
