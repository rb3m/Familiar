"""Compact, explicit units for grounded flight descriptions."""


def number(value, unit="", decimals=1) -> str:
    if value is None:
        return "unknown"
    return f"{value:.{decimals}f}{unit}"


def distance(value) -> str:
    if value is None:
        return "unknown"
    if abs(value) > 10_000:
        return number(value / 1000, " km")
    return number(value, " m")


def duration(value) -> str:
    return number(value, " s")


def resource(amount, maximum) -> str:
    if amount is None or maximum is None:
        return "unknown"
    if maximum <= 0:
        return "not carried"
    return f"{amount:.1f}/{maximum:.1f} ({amount / maximum:.0%})"
