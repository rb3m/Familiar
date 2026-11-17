"""Packaged YAML data with uncached, recursively merged user overrides."""

import os
import re
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import yaml

from .errors import ConfigError

DATA_DIR_ENV = "FAMILIAR_DATA_DIR"
_MISSING = object()


@dataclass(frozen=True)
class DataSet:
    value: Any
    sources: tuple[str, ...]


class _UniqueLoader(yaml.SafeLoader):
    """Safe YAML with duplicate keys treated as configuration errors."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict:
        self.flatten_mapping(node)
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                if key in mapping:
                    raise yaml.constructor.ConstructorError(
                        None, None, f"Duplicate mapping key {key!r}", key_node.start_mark
                    )
                mapping[key] = self.construct_object(value_node, deep=deep)
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    None, None, "Unhashable mapping key", key_node.start_mark
                ) from exc
        return mapping


def _validate_identifiers(package: str, name: str) -> None:
    for value in (package, name):
        if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9_]+", value):
            raise ConfigError(f"Invalid data identifier {value!r}")


def user_data_path(package: str, name: str, user_dir: Path | None = None) -> Path:
    """Locate an override using the same ~/.config policy on every platform."""
    _validate_identifiers(package, name)
    root = (
        Path(user_dir)
        if user_dir is not None
        else Path(os.environ.get(DATA_DIR_ENV, "~/.config/familiar/data"))
    )
    return root.expanduser() / package / f"{name}.yaml"


def _read_yaml(path: Traversable | Path, *, optional: bool = False) -> Any:
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        if optional and isinstance(exc, FileNotFoundError):
            return _MISSING
        raise ConfigError(f"Cannot read data {path}: {exc}") from exc


def _merge(base: Any, override: Any) -> Any:
    if not isinstance(override, dict):
        return override
    result = dict(base) if isinstance(base, dict) else {}
    for key, value in override.items():
        if value is None:
            result.pop(key, None)
            continue
        result[key] = _merge(result.get(key, {}), value)
    return result


def load_data(package: str, name: str, user_dir: Path | None = None) -> DataSet:
    """Load required packaged data and an optional user file, without caching."""
    user_path = user_data_path(package, name, user_dir)
    try:
        packaged_path = files(package).joinpath("data", f"{name}.yaml")
    except (ModuleNotFoundError, TypeError) as exc:
        raise ConfigError(f"Cannot locate packaged data {package}/data/{name}.yaml: {exc}") from exc
    value = _read_yaml(packaged_path)
    sources = (str(packaged_path),)
    override = _read_yaml(user_path, optional=True)
    if override is _MISSING:
        return DataSet(value, sources)
    if override is not None:
        value = _merge(value, override)
    return DataSet(value, (*sources, str(user_path)))


def read_user_data(package: str, name: str, user_dir: Path | None = None) -> Any:
    """Return only the user's override (None when absent), e.g. to update it in place."""
    value = _read_yaml(user_data_path(package, name, user_dir), optional=True)
    return None if value is _MISSING else value


def save_user_data(package: str, name: str, value: Any) -> None:
    """Atomically replace a user YAML file; readers never see partial writes."""
    path = user_data_path(package, name)
    temporary = None
    try:
        contents = yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(contents)
        temporary.replace(path)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot save data {path}: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
