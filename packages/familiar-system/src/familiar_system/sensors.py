"""Closed sensor allow-list and device-first warning lines."""

import math
import re
from dataclasses import dataclass

from familiar.interfaces import ConfigError, load_data

from familiar_system.readers import Temperature


def settings_for(overrides: dict | None = None) -> dict:
    settings = {**load_data("familiar_system", "sensors").value, **(overrides or {})}
    for key in ("thermal_interval_s", "storage_interval_s", "read_timeout_s"):
        value = settings[key]
        if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
            raise ConfigError(f"system.{key} must be a finite positive number")
    return settings


def match_rule(sensor: Temperature, settings: dict) -> dict | None:
    return next((rule for rule in settings["rules"] if _matches(sensor, rule)), None)


def _matches(sensor: Temperature, rule: dict) -> bool:
    if sensor.driver != rule["driver"] or sensor.label is None:
        return False
    label = rule["label"]
    if label.startswith("re:"):
        return re.search(label[3:], sensor.label) is not None
    return sensor.label == label


def plausible(sensor: Temperature, settings: dict) -> bool:
    value = sensor.celsius
    low, high = settings["plausible_c"]
    return value is not None and math.isfinite(value) and low <= value <= high


def warning_line(sensor: Temperature, rule: dict) -> tuple[float | None, str | None]:
    if not rule.get("alert", False):
        return None, None
    limit = rule.get("limit")
    value = {"max": sensor.max_c, "crit": sensor.crit_c}.get(limit)
    margin = rule.get("margin_c", 0)
    if value is not None and math.isfinite(value) and -50 <= value <= 200:
        suffix = " - margin" * bool(margin)
        return value - margin, f"device {limit}{suffix}"
    fallback = rule.get("fallback_c")
    if fallback is not None:
        return fallback, "fallback"
    return None, None


@dataclass(frozen=True)
class MatchedTemperature:
    sensor: Temperature
    role: str
    line_c: float | None
    line_source: str | None


def matched(sensor: Temperature, settings: dict) -> MatchedTemperature | None:
    rule = match_rule(sensor, settings)
    if rule is None or not plausible(sensor, settings):
        return None
    return MatchedTemperature(sensor, rule["role"], *warning_line(sensor, rule))


def reported(sensors: list[Temperature], settings: dict) -> list[MatchedTemperature]:
    return [item for sensor in sensors if (item := matched(sensor, settings)) is not None]
