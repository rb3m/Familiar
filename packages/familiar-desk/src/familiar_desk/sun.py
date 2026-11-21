"""Offline NOAA geometric solar position and local-day crossings."""

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo


@dataclass(frozen=True)
class SunTimes:
    sunrise: datetime | None
    sunset: datetime | None
    civil_dawn: datetime | None
    civil_dusk: datetime | None
    polar_day: bool = False
    polar_night: bool = False


def elevation(latitude: float, longitude: float, now: datetime) -> float:
    """Return geometric elevation in degrees; longitude is positive east."""
    if now.utcoffset() is None:
        raise ValueError("Solar position requires an aware datetime")
    utc = now.astimezone(UTC)
    century = (utc.timestamp() / 86400 + 2440587.5 - 2451545) / 36525
    mean_lon = (280.46646 + century * (36000.76983 + century * 0.0003032)) % 360
    anomaly = math.radians(357.52911 + century * (35999.05029 - 0.0001537 * century))
    eccentricity = 0.016708634 - century * (0.000042037 + 0.0000001267 * century)
    center = (
        math.sin(anomaly) * (1.914602 - century * (0.004817 + 0.000014 * century))
        + math.sin(2 * anomaly) * (0.019993 - 0.000101 * century)
        + math.sin(3 * anomaly) * 0.000289
    )
    omega = math.radians(125.04 - 1934.136 * century)
    apparent_lon = math.radians(mean_lon + center - 0.00569 - 0.00478 * math.sin(omega))
    obliquity = math.radians(
        23
        + (26 + (21.448 - century * (46.815 + century * (0.00059 - century * 0.001813))) / 60) / 60
        + 0.00256 * math.cos(omega)
    )
    declination = math.asin(math.sin(obliquity) * math.sin(apparent_lon))
    y = math.tan(obliquity / 2) ** 2
    lon = math.radians(mean_lon)
    equation = 4 * math.degrees(
        y * math.sin(2 * lon)
        - 2 * eccentricity * math.sin(anomaly)
        + 4 * eccentricity * y * math.sin(anomaly) * math.cos(2 * lon)
        - 0.5 * y * y * math.sin(4 * lon)
        - 1.25 * eccentricity * eccentricity * math.sin(2 * anomaly)
    )
    minutes = utc.hour * 60 + utc.minute + utc.second / 60 + utc.microsecond / 60000000
    hour_angle = math.radians(((minutes + equation + 4 * longitude) % 1440) / 4 - 180)
    lat = math.radians(latitude)
    sine = math.sin(lat) * math.sin(declination) + math.cos(lat) * math.cos(declination) * math.cos(
        hour_angle
    )
    return math.degrees(math.asin(max(-1, min(1, sine))))


def sun_times(latitude: float, longitude: float, day: date, zone: tzinfo) -> SunTimes:
    """Find crossings within a local date, including 23/25-hour DST days."""
    start = datetime.combine(day, time(), zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(), zone).astimezone(UTC)
    crossings = {"sunrise": None, "sunset": None, "civil_dawn": None, "civil_dusk": None}
    for threshold, rising, falling in (
        (-0.833, "sunrise", "sunset"),
        (-6, "civil_dawn", "civil_dusk"),
    ):
        left = start
        previous = elevation(latitude, longitude, left) - threshold
        while left < end:
            right = min(left + timedelta(minutes=5), end)
            current = elevation(latitude, longitude, right) - threshold
            if (previous < 0 <= current) or (previous >= 0 > current):
                low, high = left, right
                for _ in range(16):
                    mid = low + (high - low) / 2
                    if (elevation(latitude, longitude, mid) - threshold >= 0) == (previous >= 0):
                        low = mid
                    else:
                        high = mid
                crossings[rising if current > previous else falling] = high.astimezone(zone)
            left, previous = right, current
    no_sun = crossings["sunrise"] is None and crossings["sunset"] is None
    above = elevation(latitude, longitude, start + (end - start) / 2) >= -0.833
    return SunTimes(**crossings, polar_day=no_sun and above, polar_night=no_sun and not above)
