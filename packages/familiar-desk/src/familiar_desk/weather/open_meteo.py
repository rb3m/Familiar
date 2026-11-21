"""Open-Meteo forecast API: free, keyless, current conditions plus today's forecast."""

from datetime import datetime, timedelta, timezone

import httpx

from familiar_desk.weather import Current, Place, Today, WeatherReport, Window, worst

CURRENT = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,cloud_cover,"
    "precipitation,weather_code,wind_speed_10m"
)
HOURLY = "temperature_2m,precipitation_probability,weather_code"
DAILY = "temperature_2m_min,temperature_2m_max,precipitation_probability_max,precipitation_sum"


class OpenMeteoProvider:
    name = "open-meteo"

    def __init__(self, settings: dict) -> None:
        data = settings["open_meteo"]
        self.url = data["url"]
        self.codes = {code: name for name, codes in data["codes"].items() for code in codes}
        self.severity = settings["severity"]
        self.wet = set(settings["wet"])
        self.wet_min = data["wet_min_chance_pct"]
        self.windows = settings["windows"]

    async def fetch(self, place: Place, client: httpx.AsyncClient) -> WeatherReport:
        if place.latitude is None or place.longitude is None:
            raise ValueError("Open-Meteo needs desk latitude and longitude")
        response = await client.get(
            self.url,
            params={
                "latitude": place.latitude,
                "longitude": place.longitude,
                "timezone": place.timezone or "auto",
                "current": CURRENT,
                "hourly": HOURLY,
                "daily": DAILY,
                "forecast_days": 1,
                "wind_speed_unit": "ms",
            },
        )
        response.raise_for_status()
        body = response.json()
        zone = timezone(timedelta(seconds=body.get("utc_offset_seconds", 0)))
        now = datetime.fromisoformat(body["current"]["time"]).replace(tzinfo=zone)
        return WeatherReport(
            provider=self.name,
            current=self._current(body["current"], now),
            today=self._today(body["daily"], body["hourly"], now),
            sources=(self.name,),
        )

    def _condition(self, code) -> str:
        return self.codes.get(code, "unknown")

    def _likely(self, code, chance) -> str:
        """A wet code at a low rain chance reads as the dry sky it mostly is."""
        name = self._condition(code)
        if name in self.wet and chance is not None and chance < self.wet_min:
            return "cloudy"
        return name

    def _current(self, data: dict, now: datetime) -> Current:
        return Current(
            observed_at=now.isoformat(),
            condition=self._condition(data.get("weather_code")),
            temperature_c=data.get("temperature_2m"),
            feels_like_c=data.get("apparent_temperature"),
            humidity_pct=data.get("relative_humidity_2m"),
            cloud_cover_pct=data.get("cloud_cover"),
            precipitation_mm=data.get("precipitation"),
            wind_m_s=data.get("wind_speed_10m"),
        )

    def _today(self, daily: dict, hourly: dict, now: datetime) -> Today:
        hours = [
            (datetime.fromisoformat(stamp).hour, temp, chance, self._likely(code, chance))
            for stamp, temp, chance, code in zip(
                hourly["time"],
                hourly["temperature_2m"],
                hourly["precipitation_probability"],
                hourly["weather_code"],
                strict=True,
            )
        ]
        windows = tuple(
            window
            for name, start, end in self.windows
            if end > now.hour
            for window in [self._window(name, hours, max(start, now.hour), end)]
            if window is not None
        )
        return Today(
            date=daily["time"][0],
            min_c=daily["temperature_2m_min"][0],
            max_c=daily["temperature_2m_max"][0],
            rain_chance_pct=daily["precipitation_probability_max"][0],
            precipitation_mm=daily["precipitation_sum"][0],
            condition=worst((hour[3] for hour in hours), self.severity),
            windows=windows,
        )

    def _window(self, name: str, hours: list, start: int, end: int) -> Window | None:
        slot = [hour for hour in hours if start <= hour[0] < end]
        if not slot:
            return None
        temps = [hour[1] for hour in slot if hour[1] is not None]
        chances = [hour[2] for hour in slot if hour[2] is not None]
        return Window(
            name=name,
            condition=worst((hour[3] for hour in slot), self.severity),
            min_c=min(temps, default=None),
            max_c=max(temps, default=None),
            rain_chance_pct=max(chances, default=None),
        )
