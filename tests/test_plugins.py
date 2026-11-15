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
    api_version = "1.7"

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
    monkeypatch.setattr(FakePlugin, "api_version", "2.0")
    with pytest.raises(PluginError, match="incompatible API version"):
        plugins.load("llm", "fake", {})
