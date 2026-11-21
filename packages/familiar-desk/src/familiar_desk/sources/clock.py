"""Local clock and optional solar context."""

from datetime import datetime
from zoneinfo import ZoneInfo

from familiar_desk.sun import elevation, sun_times


class ClockSource:
    name = "clock"

    def __init__(self, settings: dict) -> None:
        self.interval_s = settings.get("interval_s", 60)
        self.zone = ZoneInfo(settings["timezone"]) if settings.get("timezone") else None
        self.latitude = settings.get("latitude")
        self.longitude = settings.get("longitude")
        self._period = None
        self._sunset_date = None

    async def sample(self, now: datetime) -> tuple[dict, list[dict]]:
        local = now.astimezone(self.zone)
        hour = local.hour + local.minute / 60
        period = next(
            name
            for limit, name in (
                (5, "night"),
                (6, "dawn"),
                (11, "morning"),
                (14, "midday"),
                (18, "afternoon"),
                (19, "dusk"),
                (22, "evening"),
                (24, "late_night"),
            )
            if hour < limit
        )
        data = {
            "local_time": local.isoformat(),
            "date": local.date().isoformat(),
            "weekday": local.strftime("%A"),
            "weekend": local.weekday() >= 5,
            "timezone": getattr(local.tzinfo, "key", None) or local.strftime("%z"),
        }
        events = []
        if self.latitude is not None and self.longitude is not None:
            times = sun_times(self.latitude, self.longitude, local.date(), local.tzinfo)
            height = elevation(self.latitude, self.longitude, local)
            data.update(
                {
                    key: value.isoformat() if value else None
                    for key, value in (
                        ("sunrise", times.sunrise),
                        ("sunset", times.sunset),
                        ("civil_dawn", times.civil_dawn),
                        ("civil_dusk", times.civil_dusk),
                    )
                }
            )
            data.update(elevation=height, polar_day=times.polar_day, polar_night=times.polar_night)
            if height < -6:
                period = "night" if hour < 12 else "evening"
                if hour >= 22:
                    period = "late_night"
            elif height < -0.833:
                period = "dawn" if times.sunrise and local < times.sunrise else "dusk"
            else:
                period = "morning" if hour < 11 else "midday" if hour < 14 else "afternoon"
            if times.sunset:
                remaining = (times.sunset - local).total_seconds()
                if 0 < remaining <= 1800 and self._sunset_date != data["date"]:
                    events.append(
                        self._event(
                            local, "sunset_soon", "Sunset is about half an hour away.", 1800
                        )
                    )
                    self._sunset_date = data["date"]
        data["period"] = period
        if self._period is not None and self._period != period:
            events.append(
                self._event(
                    local, f"period:{period}", f"It is now {period.replace('_', ' ')}.", 600
                )
            )
        self._period = period
        return data, events

    @staticmethod
    def _event(now: datetime, kind: str, summary: str, ttl: int) -> dict:
        parts = kind.split(":")
        suffix = f":{parts[1]}" if len(parts) > 1 else ""
        return {
            "id": f"clock:{parts[0]}:{now.date().isoformat()}{suffix}",
            "ts": now.isoformat(),
            "kind": f"clock:{parts[0]}",
            "priority": "ambient",
            "summary": summary,
            "ttl_s": ttl,
        }
