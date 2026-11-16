"""Presentation conversions; all inputs remain km, km/h, litres and kg.

Text goes to the LLM and from there to text-to-speech, so units are full words: an abbreviation
like "6 h 35 min" was read aloud letter by letter.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from familiar.interfaces import ConfigError


@dataclass(frozen=True)
class Units:
    system: Literal["imperial", "metric"] = "imperial"
    game: str = "ats"

    def __post_init__(self) -> None:
        if self.system not in {"imperial", "metric"}:
            raise ConfigError(f"Unknown units: {self.system!r}")

    def _format(self, value: float, divisor: float, imperial: str, metric: str) -> str:
        if self.system == "imperial":
            return quantity(value / divisor, imperial)
        return quantity(value, metric)

    def speed(self, kmh: float) -> str:
        return self._format(kmh, 1.609344, "mile per hour", "kilometer per hour")

    def distance(self, km: float) -> str:
        return self._format(km, 1.609344, "mile", "kilometer")

    def volume(self, litres: float) -> str:
        return self._format(litres, 3.785411784, "gallon", "liter")

    def money(self, amount: float) -> str:
        symbol = {"ats": "$", "ets2": "€"}.get(self.game, "$")
        return f"{symbol}{amount:,.0f}"

    def mass(self, kg: float) -> str:
        if self.system == "imperial":
            return quantity(kg * 2.20462, "pound")  # pounds, as the ATS HUD shows mass
        return quantity(kg / 1000, "tonne", decimals=1)


PLURALS = {"mile per hour": "miles per hour", "kilometer per hour": "kilometers per hour"}


def quantity(value: float, unit: str, decimals: int = 0) -> str:
    """'1 mile', '79 gallons', '22.5 tonnes', with thousands separators."""
    rounded = round(value, decimals)
    number = f"{rounded:,.{decimals}f}"
    if decimals and number.endswith("0"):
        number = f"{rounded:,.0f}" if rounded == int(rounded) else number.rstrip("0")
    word = unit if rounded == 1 else PLURALS.get(unit, unit + "s")
    return f"{number} {word}"


def duration_text(value: timedelta | None) -> str:
    if value is None:
        return "unknown"
    minutes = max(0, round(value.total_seconds() / 60))
    hours, minutes = divmod(minutes, 60)
    if not hours:
        return quantity(minutes, "minute")
    if not minutes:
        return quantity(hours, "hour")
    return f"{quantity(hours, 'hour')} {quantity(minutes, 'minute')}"


def rest_text(remaining: timedelta) -> str:
    if remaining <= timedelta(0):
        return "Rest is overdue: the driver needs to sleep now"
    return f"Rest needed in {duration_text(remaining)} (game time)"
