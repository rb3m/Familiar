"""Packaged defaults only; never consult machine-specific sensor overrides."""

from importlib.resources import files
from types import SimpleNamespace

import pytest
import yaml
from familiar_system import sensors


@pytest.fixture
def settings(monkeypatch):
    defaults = yaml.safe_load(files("familiar_system").joinpath("data/sensors.yaml").read_text())
    monkeypatch.setattr(sensors, "load_data", lambda *args: SimpleNamespace(value=defaults))
    return sensors.settings_for()
