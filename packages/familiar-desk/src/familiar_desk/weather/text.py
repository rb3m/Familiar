"""Plain-English weather phrases for prompts and remarks, from feed (dict) data."""

BACKGROUND = "background only: don't bring it up unless asked or it matters"


def condition(name: str | None) -> str:
    return (name or "unknown").replace("_", " ")


def degrees(value) -> str:
    return f"{value:.0f} °C"


def span(low, high) -> str:
    if low is None or high is None:
        return ""
    if round(low) == round(high):
        return degrees(low)
    return f"{low:.0f}–{high:.0f} °C"


def now_phrase(current: dict) -> str:
    parts = [condition(current.get("condition"))]
    if current.get("temperature_c") is not None:
        parts.insert(0, degrees(current["temperature_c"]))
    feels, actual = current.get("feels_like_c"), current.get("temperature_c")
    if feels is not None and actual is not None and abs(feels - actual) >= 2:
        parts.append(f"feels like {degrees(feels)}")
    for key, label in (("cloud_cover_pct", "cloud"), ("humidity_pct", "humidity")):
        if current.get(key) is not None:
            parts.append(f"{label} {current[key]:.0f}%")
    if current.get("precipitation_mm"):
        parts.append(f"{current['precipitation_mm']:.1f} mm precipitation lately")
    if current.get("wind_m_s") is not None and current["wind_m_s"] >= 5:
        parts.append(f"wind {current['wind_m_s']:.0f} m/s")
    return ", ".join(parts)


def window_phrase(window: dict) -> str:
    text = f"{window['name']} {condition(window['condition'])}"
    temps = span(window.get("min_c"), window.get("max_c"))
    if temps:
        text += f" {temps}"
    if window.get("rain_chance_pct"):
        text += f" ({window['rain_chance_pct']:.0f}% rain chance)"
    return text


def today_phrase(today: dict) -> str:
    parts = [f"today {condition(today.get('condition'))}"]
    temps = span(today.get("min_c"), today.get("max_c"))
    if temps:
        parts.append(f"low/high {temps}")
    if today.get("rain_chance_pct") is not None:
        parts.append(f"rain chance up to {today['rain_chance_pct']:.0f}%")
    text = ", ".join(parts)
    windows = "; ".join(window_phrase(window) for window in today.get("windows") or ())
    return f"{text}; {windows}" if windows else text


def where(location: str | None) -> str:
    return f" in {location}" if location else ""


def describe(current: dict | None, today: dict | None, location: str | None) -> str:
    phrases = [now_phrase(current) if current else "", today_phrase(today) if today else ""]
    if not any(phrases):
        return ""
    if not today:
        phrases.append("no forecast available")
    body = ". ".join(phrase[0].upper() + phrase[1:] for phrase in phrases if phrase)
    return f"Weather{where(location)} ({BACKGROUND}): {body}."
