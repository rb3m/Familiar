"""Core remains usable with no installed game pack."""

import json
import subprocess
import sys
import textwrap

from familiar import cli, plugins
from familiar.config import AppConfig
from familiar.gui import server


def test_all_core_modules_import_without_game_pack():
    # A fresh interpreter avoids SCS modules collected by the workspace's other tests.
    script = textwrap.dedent("""
        import importlib
        import importlib.abc
        import importlib.metadata
        import pkgutil
        import sys

        class NoGamePack(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "familiar_scs" or fullname.startswith("familiar_scs."):
                    raise AssertionError(f"Core imported {fullname}")

        sys.meta_path.insert(0, NoGamePack())
        importlib.metadata.entry_points = lambda **kwargs: []
        import familiar
        for module in pkgutil.walk_packages(familiar.__path__, "familiar."):
            importlib.import_module(module.name)
        assert not any(n == "familiar_scs" or n.startswith("familiar_scs.") for n in sys.modules)
    """)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_plugins_without_entry_points(monkeypatch, capsys):
    monkeypatch.setattr(plugins, "entry_points", lambda **kwargs: [])
    monkeypatch.setattr(sys, "argv", ["familiar", "plugins"])
    cli.main()
    assert "games: (none)" in capsys.readouterr().out


def test_gui_without_entry_points(monkeypatch, tmp_path):
    monkeypatch.setattr(plugins, "entry_points", lambda **kwargs: [])
    observed = []

    async def serve(port, no_browser, config_path, session_path=None):
        app = server.create_app(AppConfig(user_personas_dir=tmp_path), token="test")
        state = json.loads((await app[server.API].state(None)).body)
        assert state["games"] == []
        assert state["defaults"]["game"] is None
        assert any(p["id"] == "mira" for p in state["personas"])
        assert server.LOGO.is_file()
        observed.append((port, no_browser, config_path))

    monkeypatch.setattr(server, "serve", serve)
    monkeypatch.setattr(sys, "argv", ["familiar", "gui", "--no-browser"])
    cli.main()
    assert observed == [(8765, True, None)]
