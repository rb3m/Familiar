"""Presentation conversions; all inputs remain km, km/h, litres and kg."""

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
            return f"{value / divisor:.0f} {imperial}"
        return f"{value:.0f} {metric}"

    def speed(self, kmh: float) -> str:
        return self._format(kmh, 1.609344, "mph", "km/h")

    def distance(self, km: float) -> str:
        return self._format(km, 1.609344, "mi", "km")

    def volume(self, litres: float) -> str:
        return self._format(litres, 3.785411784, "gal", "L")

    def money(self, amount: float) -> str:
        symbol = {"ats": "$", "ets2": "€"}.get(self.game, "$")
        return f"{symbol}{amount:,.0f}"

    def mass(self, kg: float) -> str:
        if self.system == "imperial":
            return f"{kg / 907.18474:.1f} short tons"
        return f"{kg / 1000:g} t"


def duration_text(value: timedelta | None) -> str:
    if value is None:
        return "unknown"
    minutes = max(0, round(value.total_seconds() / 60))
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes} min"
    return f"{minutes} min"
