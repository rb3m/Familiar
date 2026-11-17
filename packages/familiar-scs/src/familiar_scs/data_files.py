"""Shared validation helpers for the SCS pack's overrideable data."""

from math import isfinite

from familiar.interfaces import ConfigError, load_data


def mapping_data(name: str) -> tuple[dict, str]:
    data = load_data("familiar_scs", name)
    source = ", ".join(data.sources)
    require(isinstance(data.value, dict), source, "expected a mapping")
    return data.value, source


def require(valid: bool, source: str, message: str) -> None:
    if not valid:
        raise ConfigError(f"Invalid data {source}: {message}")


def text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def number(value: object) -> bool:
    return type(value) in (int, float) and isfinite(value)


def string_mapping(value: object) -> bool:
    return isinstance(value, dict) and all(text(k) and text(v) for k, v in value.items())
