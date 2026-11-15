"""Distances and directions, with the telemetry heading convention isolated."""

from math import asin, atan2, cos, degrees, radians, sin, sqrt

EARTH_RADIUS_KM = 6370.997
# Assumption pending live verification: zero north, increasing clockwise.
HEADING_ZERO_DEGREES = 0.0
HEADING_DIRECTION = 1.0
COMPASS_POINTS = (
    "north",
    "northeast",
    "east",
    "southeast",
    "south",
    "southwest",
    "west",
    "northwest",
)


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    a, b = radians(lat1), radians(lat2)
    h = sin((b - a) / 2) ** 2 + cos(a) * cos(b) * sin(radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(min(1.0, max(0.0, h))))


def heading_degrees(heading: float) -> float:
    return (HEADING_ZERO_DEGREES + HEADING_DIRECTION * heading * 360) % 360


def compass_point(heading: float) -> str:
    return COMPASS_POINTS[int((heading_degrees(heading) + 22.5) / 45) % 8]


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    a, b, d = radians(lat1), radians(lat2), radians(lon2 - lon1)
    return degrees(atan2(sin(d) * cos(b), cos(a) * sin(b) - sin(a) * cos(b) * cos(d))) % 360


def bearing_difference(a: float, b: float) -> float:
    return (b - a + 180) % 360 - 180
