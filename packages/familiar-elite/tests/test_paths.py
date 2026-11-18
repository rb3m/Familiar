"""Proton discovery including modern and legacy extra Steam libraries."""

from pathlib import Path

from familiar_elite.paths import find_paths


def test_overrides(tmp_path):
    paths = find_paths({"journal_dir": str(tmp_path), "bindings_dir": "~/binds"})
    assert paths.journal_dir == tmp_path
    assert paths.bindings_dir == Path("~/binds").expanduser()


def test_libraries_and_data_override(tmp_path, monkeypatch):
    data = tmp_path / "data/familiar_elite"
    data.mkdir(parents=True)
    root = tmp_path / "steam"
    (root / "steamapps").mkdir(parents=True)
    external = tmp_path / "external library"
    (root / "steamapps/libraryfolders.vdf").write_text(
        f'"libraryfolders" {{ "0" {{ "path" "{external}" }} }}'
    )
    (data / "paths.yaml").write_text(f'steam_roots: ["{root}"]\n')
    prefix = external / "steamapps/compatdata/359320/pfx/drive_c/users/steamuser"
    journal = prefix / "Saved Games/Frontier Developments/Elite Dangerous"
    bindings = prefix / "AppData/Local/Frontier Developments/Elite Dangerous/Options/Bindings"
    journal.mkdir(parents=True)
    bindings.mkdir(parents=True)
    monkeypatch.setenv("FAMILIAR_DATA_DIR", str(data.parent))
    assert find_paths({}).journal_dir == journal
    assert find_paths({}).bindings_dir == bindings
    (root / "steamapps/libraryfolders.vdf").write_text(f'"1" "{external}"')
    assert find_paths({}).journal_dir == journal
    (root / "steamapps/libraryfolders.vdf").unlink()
    assert find_paths({}).journal_dir is None


def test_control_schemes_prefers_odyssey_across_libraries(tmp_path, monkeypatch):
    data = tmp_path / "data/familiar_elite"
    data.mkdir(parents=True)
    root = tmp_path / "steam"
    external = tmp_path / "external"
    (root / "steamapps").mkdir(parents=True)
    (root / "steamapps/libraryfolders.vdf").write_text(f'"1" "{external}"')
    (data / "paths.yaml").write_text(f'steam_roots: ["{root}"]\n')
    products = Path("steamapps/common/Elite Dangerous/Products")
    legacy = root / products / "legacy" / "ControlSchemes"
    preferred = external / products / "elite-dangerous-odyssey-64" / "ControlSchemes"
    legacy.mkdir(parents=True)
    preferred.mkdir(parents=True)
    monkeypatch.setenv("FAMILIAR_DATA_DIR", str(data.parent))
    assert find_paths({}).control_schemes_dir == preferred
    assert (
        find_paths({"control_schemes_dir": "~/schemes"}).control_schemes_dir
        == Path("~/schemes").expanduser()
    )
    preferred.rmdir()
    assert find_paths({}).control_schemes_dir == legacy
