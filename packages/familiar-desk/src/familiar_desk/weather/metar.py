"""Airport METAR from aviationweather.gov: free, keyless, current conditions only."""

import math
from datetime import UTC, datetime

import httpx

from familiar_desk.weather import Current, Place, WeatherReport

KNOT_M_S = 0.514444


class MetarProvider:
    name = "metar"

    def __init__(self, settings: dict) -> None:
        data = settings["metar"]
        self.url = data["url"]
        self.weather = data["weather"]
        self.cover = {code: name for name, codes in data["cover"].items() for code in codes}
        self.cover_pct = data["cover_pct"]
        self.max_km = settings["max_station_km"]

    async def fetch(self, place: Place, client: httpx.AsyncClient) -> WeatherReport:
        if not place.station:
            raise ValueError("METAR needs desk.weather.station (an ICAO code)")
        response = await client.get(self.url, params={"ids": place.station, "format": "json"})
        response.raise_for_status()
        reports = response.json()
        if not reports:
            raise ValueError(f"No METAR for {place.station}")
        data = reports[0]
        self._check_distance(place, data)
        return WeatherReport(provider=self.name, current=self._current(data), sources=(self.name,))

    def _check_distance(self, place: Place, data: dict) -> None:
        if place.latitude is None or data.get("lat") is None:
            return
        distance = haversine_km(place.latitude, place.longitude, data["lat"], data["lon"])
        if distance > self.max_km:
            raise ValueError(
                f"METAR station {place.station} is {distance:.0f} km from the desk location; "
                "set desk.weather.station to a nearby airport"
            )

    def _current(self, data: dict) -> Current:
        cover = data.get("cover")
        temperature, dew = data.get("temp"), data.get("dewp")
        humidity = relative_humidity(temperature, dew)
        wind = data.get("wspd")
        return Current(
            observed_at=datetime.fromtimestamp(data["obsTime"], UTC).isoformat(),
            condition=self._condition(data.get("wxString") or "", cover),
            temperature_c=temperature,
            humidity_pct=humidity,
            cloud_cover_pct=self.cover_pct.get(cover),
            wind_m_s=round(wind * KNOT_M_S, 1) if isinstance(wind, int | float) else None,
        )

    def _condition(self, weather: str, cover: str | None) -> str:
        present = next(
            (name for name, codes in self.weather.items() if any(c in weather for c in codes)),
            None,
        )
        return present or self.cover.get(cover, "unknown")


def relative_humidity(temperature: float | None, dew: float | None) -> float | None:
    """Magnus formula."""
    if temperature is None or dew is None:
        return None

    def vapour(celsius):
        return math.exp(17.625 * celsius / (243.04 + celsius))

    return round(100 * vapour(dew) / vapour(temperature))


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlam = phi2 - phi1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))
