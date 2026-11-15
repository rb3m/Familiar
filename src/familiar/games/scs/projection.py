"""SCS spherical Lambert conformal conic inverse projections."""

from math import atan, atan2, cos, floor, hypot, log, pi, radians, tan

RADIUS_M = 6370997.0
LENGTH_OF_DEGREE = RADIUS_M * pi / 180


def _inverse(
    x: float, y: float, parallels: tuple[float, float, float, float]
) -> tuple[float, float]:
    p1, p2, p0, lon0 = map(radians, parallels)
    n = log(cos(p1) / cos(p2)) / log(tan(pi / 4 + p2 / 2) / tan(pi / 4 + p1 / 2))
    f = cos(p1) * tan(pi / 4 + p1 / 2) ** n / n
    rho0 = RADIUS_M * f / tan(pi / 4 + p0 / 2) ** n
    rho = hypot(x, rho0 - y)
    if rho == 0:
        return 90.0, lon0 * 180 / pi
    lat = 2 * atan((RADIUS_M * f / rho) ** (1 / n)) - pi / 2
    lon = lon0 + atan2(x, rho0 - y) / n
    return lat * 180 / pi, lon * 180 / pi


def ats_to_wgs84(x: float, z: float) -> tuple[float, float]:
    return _inverse(
        x * 0.000176689948 * LENGTH_OF_DEGREE,
        z * -0.00017706234 * LENGTH_OF_DEGREE,
        (33, 45, 39, -96),
    )


def ets2_to_wgs84(x: float, z: float) -> tuple[float, float]:
    sx, sy = floor(x / 4000), floor(z / 4000)
    x, z = x - 16660, z - 4150
    if sx <= -8 and sy <= -2 and (sx, sy) != (-8, -2):
        x, z = (x - 31100 / 2) * 0.75, (z - 5500 / 2) * 0.75
    return _inverse(
        x * 0.0001729241463 * LENGTH_OF_DEGREE,
        z * -0.000171570875 * LENGTH_OF_DEGREE,
        (37, 65, 50, 15),
    )


PROJECTIONS = {"ats": ats_to_wgs84, "ets2": ets2_to_wgs84}
