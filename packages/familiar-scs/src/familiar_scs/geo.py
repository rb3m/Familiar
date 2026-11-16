"""Distances and directions, with the telemetry heading convention isolated."""

from collections.abc import Callable
from math import asin, atan2, cos, degrees, radians, sin, sqrt, tau

EARTH_RADIUS_KM = 6370.997
SOLSTICE_DECLINATION_DEG = 23.44
# Solar noon is unverified (12:00 vs DST); calibrate this assumption by observation.
SOLAR_NOON_HOUR = 13.0
NIGHT_ELEVATION_DEG = -6.0
DAY_ELEVATION_DEG = 6.0
# SCS SDK (scssdk_value.h): heading in <0,1), measured counterclockwise seen from above;
# 0 = north, 0.25 = west, 0.5 = south, 0.75 = east. Game north is -z, west is -x.
HEADING_ZERO_DEGREES = 0.0
HEADING_DIRECTION = -1.0
LOOK_AHEAD_UNITS = 100.0
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


def sun_elevation(lat_deg: float, game_hour_float: float) -> float:
    """June-solstice sun height using the game clock, without a longitude correction."""
    latitude = radians(lat_deg)
    declination = radians(SOLSTICE_DECLINATION_DEG)
    hour_angle = radians(15 * (game_hour_float - SOLAR_NOON_HOUR))
    height = sin(latitude) * sin(declination) + cos(latitude) * cos(declination) * cos(hour_angle)
    return degrees(asin(max(-1.0, min(1.0, height))))


def day_period(elevation: float, game_hour_float: float) -> str:
    """Classify light level; the clock disambiguates morning and evening twilight."""
    if elevation < NIGHT_ELEVATION_DEG:
        return "night"
    if elevation >= DAY_ELEVATION_DEG:
        return "day"
    # Split twilight at solar midnight, not noon: 00:30 at high latitude is still dusk.
    return "dawn" if (game_hour_float - SOLAR_NOON_HOUR) % 24 >= 12 else "dusk"


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    a, b = radians(lat1), radians(lat2)
    h = sin((b - a) / 2) ** 2 + cos(a) * cos(b) * sin(radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(min(1.0, max(0.0, h))))


def heading_degrees(heading: float) -> float:
    return (HEADING_ZERO_DEGREES + HEADING_DIRECTION * heading * 360) % 360


def compass_name(degrees_from_north: float) -> str:
    return COMPASS_POINTS[int((degrees_from_north % 360 + 22.5) / 45) % 8]


def compass_point(heading: float) -> str:
    """Compass point on the game map (map north, not true north)."""
    return compass_name(heading_degrees(heading))


def true_heading(
    projection: Callable[[float, float], tuple[float, float]], x: float, z: float, heading: float
) -> float:
    """Bearing from true north: project a point just ahead of the truck to lat/lon.

    The map is a conic projection, so map north drifts from true north away from its central
    meridian (about 15 degrees in eastern Oregon).
    """
    angle = heading * tau
    ahead_x = x - sin(angle) * LOOK_AHEAD_UNITS
    ahead_z = z - cos(angle) * LOOK_AHEAD_UNITS
    return bearing(*projection(x, z), *projection(ahead_x, ahead_z))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    a, b, d = radians(lat1), radians(lat2), radians(lon2 - lon1)
    return degrees(atan2(sin(d) * cos(b), cos(a) * sin(b) - sin(a) * cos(b) * cos(d))) % 360


def bearing_difference(a: float, b: float) -> float:
    return (b - a + 180) % 360 - 180
