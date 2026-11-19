"""Formatting helpers for quantities shown to language models."""

from decimal import ROUND_HALF_UP, Decimal

from .data import load_data


def _quantity(value: Decimal, unit: str, units: dict) -> str:
    magnitude = abs(value)
    decimals = 0
    if magnitude < 100:
        decimals = 1
    if 0 < magnitude < 10:
        decimals = 1 - magnitude.adjusted()
    rounded = value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    number = f"{rounded:,.{decimals}f}"
    if decimals:
        number = number.rstrip("0").rstrip(".")
    singular, plural = units[unit]
    word = singular if abs(rounded) == 1 else plural
    return f"{number} {word}"


def spoken(value: float | None, unit: str) -> str:
    """Round a quantity with explicit English units; reload user overrides on each call."""
    units = load_data("familiar", "units").value
    if unit not in units:
        raise ValueError(f"Unknown unit: {unit}")
    if value is None:
        return "unknown"
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError("Quantity must be finite")
    if unit == "s":
        return _duration(amount, units)
    return _quantity(amount, unit, units)


def _duration(value: Decimal, units: dict) -> str:
    seconds = int(abs(value).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = [
        _quantity(Decimal(amount), unit, units)
        for amount, unit in ((hours, "h"), (minutes, "min"), (seconds, "s"))
        if amount
    ]
    sign = "-" if value < 0 and parts else ""
    return sign + (" ".join(parts) or _quantity(Decimal(0), "s", units))
