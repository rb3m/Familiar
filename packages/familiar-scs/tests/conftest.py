"""Keep data overrides independent of the developer's own setup."""

import pytest


@pytest.fixture(autouse=True)
def isolated_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("FAMILIAR_DATA_DIR", str(tmp_path / "data"))
