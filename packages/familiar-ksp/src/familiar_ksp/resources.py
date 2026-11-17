"""Packaged and user-overridden KSP resource mappings and display names."""

import re
from dataclasses import dataclass
from pathlib import Path

from familiar.interfaces import ConfigError, load_data


@dataclass(frozen=True)
class ResourceConfig:
    vessel: dict[str, str]
    stage: dict[str, str]
    fallback_propellants: tuple[str, ...]
    display_names: dict[str, str]


def split_camel_case(name: str) -> str:
    """Split CamelCase words into space-separated lowercase words."""
    if not name:
        return ""
    spaced_capitals = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", name)
    spaced_words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", spaced_capitals)
    return spaced_words.lower().strip()


def display_name(name: str, display_names: dict[str, str] | None = None) -> str:
    """Return a spoken English name for a resource, splitting CamelCase if unknown."""
    known = display_names if display_names is not None else load_resources().display_names
    if name in known:
        return known[name]
    return split_camel_case(name)


spoken_resource_name = display_name


def _validate_string_mapping(mapping: object, name: str, source: str) -> dict[str, str]:
    if not isinstance(mapping, dict):
        raise ConfigError(f"Resources {name} must be a mapping in {source}")
    if any(
        not isinstance(k, str) or not k.strip() or not isinstance(v, str) or not v.strip()
        for k, v in mapping.items()
    ):
        raise ConfigError(f"Resources {name} entries must be non-empty strings in {source}")
    return mapping


def load_resources(user_dir: Path | None = None) -> ResourceConfig:
    """Load and validate resource definitions with optional user overrides."""
    dataset = load_data("familiar_ksp", "resources", user_dir=user_dir)
    source = ", ".join(dataset.sources)
    if not isinstance(dataset.value, dict):
        raise ConfigError(f"Resources must be a mapping in {source}")

    vessel = _validate_string_mapping(dataset.value.get("vessel"), "vessel", source)
    if "electric_charge" not in vessel or "liquid_fuel" not in vessel:
        raise ConfigError(f"Resources vessel requires electric_charge and liquid_fuel in {source}")

    stage = _validate_string_mapping(dataset.value.get("stage"), "stage", source)

    fallback = dataset.value.get("fallback_propellants")
    if (
        not isinstance(fallback, (list, tuple))
        or not fallback
        or any(not isinstance(p, str) or not p.strip() for p in fallback)
    ):
        raise ConfigError(
            f"Resources fallback_propellants must be a non-empty list of strings in {source}"
        )

    display = _validate_string_mapping(dataset.value.get("display_names"), "display_names", source)

    return ResourceConfig(
        vessel=vessel,
        stage=stage,
        fallback_propellants=tuple(fallback),
        display_names=display,
    )
