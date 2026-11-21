"""Hourly weather from pluggable providers, with sparing remarks."""

from dataclasses import asdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from familiar_desk.weather import (
    Place,
    WeatherReport,
    load_providers,
    text,
    user_agent,
    weather_settings,
)

PLACE_KEYS = ("latitude", "longitude", "timezone", "location")


def place_for(desk: dict, weather: dict) -> Place:
    """Weather may name its own place; otherwise it reports on the desk's."""
    values = {key: weather.get(key, desk.get(key)) for key in PLACE_KEYS}
    return Place(**values, station=weather.get("station"))


class WeatherSource:
    name = "weather"

    def __init__(self, settings: dict, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = weather_settings(settings.get("weather"))
        self.place = place_for(settings, self.settings)
        self.zone = ZoneInfo(self.place.timezone) if self.place.timezone else None
        self.interval_s = self.settings["interval_s"]
        self.providers = load_providers(self.settings)
        self.transport = transport
        self._previous = None
        self._outlook_date = None
        self._last_report = None

    async def sample(self, now: datetime) -> tuple[dict, list[dict]]:
        report, errors = await self._fetch(now)
        data = {
            **asdict(report),
            "location": self.place.location,
            "errors": errors,
        }
        local = now.astimezone(self.zone)
        events = self._events(local, data)
        self._previous = data["current"]
        return data, events

    async def _fetch(self, now: datetime) -> tuple[WeatherReport, list[str]]:
        current, today, used, errors = None, None, [], []
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=self.settings["timeout_s"],
            headers={"User-Agent": user_agent()},
        ) as client:
            for provider in self.providers:
                if current is not None and today is not None:
                    break
                try:
                    report = await provider.fetch(self.place, client)
                except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as exc:
                    errors.append(f"{provider.name}: {exc}")
                    continue
                fresh = report.current if self._fresh(report, now) else None
                if report.current is not None and fresh is None:
                    errors.append(f"{provider.name}: observation is stale")
                taken = (current is None and fresh, today is None and report.today)
                current = current or fresh
                today = today or report.today
                used += [provider.name] if any(taken) else []
        if current is None and today is None:
            raise ValueError("; ".join(errors) or "No weather provider configured")
        return WeatherReport(used[0], current, today, tuple(used)), errors

    def _fresh(self, report: WeatherReport, now: datetime) -> bool:
        if report.current is None:
            return False
        observed = datetime.fromisoformat(report.current.observed_at)
        return (now - observed).total_seconds() <= self.settings["max_age_s"]

    def _events(self, local: datetime, data: dict) -> list[dict]:
        start, end = self.settings["active_hours"]
        if not start <= local.hour < end:
            return []
        outlook = self._outlook(local, data)
        change = self._change(local, data)
        report = None if outlook else self._report(local, data)
        if self._last_report is None or outlook or report:
            self._last_report = local  # temperature last given (or first heard)
        return [item for item in (outlook, change, report) if item is not None]

    def _outlook(self, local: datetime, data: dict) -> dict | None:
        today = data["today"]
        date = local.date().isoformat()
        due = self.settings["outlook_hour"] <= local.hour < 12 and self._outlook_date != date
        if today is None or not due:
            return None
        self._outlook_date = date
        summary = f"Today's weather{text.where(self.place.location)}: {text.today_phrase(today)}."
        return event(local, "outlook", date, summary, 3600)

    def _change(self, local: datetime, data: dict) -> dict | None:
        previous, current = self._previous, data["current"]
        if previous is None or current is None:
            return None
        wet = self.settings["wet"]
        was, now = previous["condition"] in wet, current["condition"] in wet
        storm = current["condition"] == "thunderstorm" != previous["condition"]
        if was == now and not storm:
            return None
        where = text.where(self.place.location)
        summary = (
            f"It has started to {text.condition(current['condition'])}{where}."
            if now
            else f"The {text.condition(previous['condition'])} has stopped{where}."
        )
        if storm:
            summary = f"A thunderstorm has started{where}."
        return event(local, "change", f"{local:%Y-%m-%dT%H:%M}", summary, 1800)

    def _report(self, local: datetime, data: dict) -> dict | None:
        every = self.settings["remark_every_h"]
        current = data["current"]
        if not every or current is None or self._last_report is None:
            return None
        if local - self._last_report < timedelta(hours=every):
            return None
        summary = f"Weather update{text.where(self.place.location)}: {text.now_phrase(current)}."
        return event(local, "report", f"{local:%Y-%m-%dT%H}", summary, 1800)


def event(local: datetime, kind: str, slot: str, summary: str, ttl: int) -> dict:
    return {
        "id": f"weather:{kind}:{slot}",
        "ts": local.isoformat(),
        "kind": f"weather:{kind}",
        "priority": "ambient",
        "summary": summary,
        "ttl_s": ttl,
    }
