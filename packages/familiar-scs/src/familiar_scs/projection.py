"""SCS spherical Lambert conformal conic inverse projections."""

from functools import partial
from math import atan, atan2, cos, floor, hypot, log, pi, radians, tan

from .data_files import mapping_data, number, require

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


def load_projections() -> dict:
    data, source = mapping_data("projections")
    require(set(data) == {"ats", "ets2"}, source, "expected ats and ets2 projections")
    required = {"scale_x", "scale_z", "parallels", "offset_x", "offset_z"}
    for game, parameters in data.items():
        require(isinstance(parameters, dict), source, f"{game}: expected mapping")
        require(
            required <= parameters.keys() and not parameters.keys() - required - {"uk"},
            source,
            f"{game}: invalid projection keys",
        )
        require(
            all(number(parameters[k]) for k in required - {"parallels"}),
            source,
            f"{game}: invalid numeric parameters",
        )
        parallels = parameters["parallels"]
        require(
            isinstance(parallels, list)
            and len(parallels) == 4
            and all(number(v) for v in parallels),
            source,
            f"{game}: invalid parallels",
        )
        if "uk" in parameters:
            uk = parameters["uk"]
            keys = {
                "sector_size",
                "max_sector_x",
                "max_sector_z",
                "excluded_sector",
                "offset_x",
                "offset_z",
                "scale",
            }
            require(isinstance(uk, dict) and set(uk) == keys, source, f"{game}: invalid UK rule")
            require(
                all(number(uk[k]) for k in keys - {"excluded_sector"}) and uk["sector_size"] > 0,
                source,
                f"{game}: invalid UK numbers",
            )
            require(
                isinstance(uk["excluded_sector"], list)
                and len(uk["excluded_sector"]) == 2
                and all(number(v) for v in uk["excluded_sector"]),
                source,
                f"{game}: invalid excluded sector",
            )
    return data


def _project(parameters: dict, x: float, z: float) -> tuple[float, float]:
    uk = parameters.get("uk")
    sector = None
    if uk:
        sector = floor(x / uk["sector_size"]), floor(z / uk["sector_size"])
    x, z = x - parameters["offset_x"], z - parameters["offset_z"]
    if (
        uk
        and sector[0] <= uk["max_sector_x"]
        and sector[1] <= uk["max_sector_z"]
        and list(sector) != uk["excluded_sector"]
    ):
        x, z = (x - uk["offset_x"]) * uk["scale"], (z - uk["offset_z"]) * uk["scale"]
    return _inverse(
        x * parameters["scale_x"] * LENGTH_OF_DEGREE,
        z * parameters["scale_z"] * LENGTH_OF_DEGREE,
        parameters["parallels"],
    )


def projection_for_game(game: str):
    return partial(_project, load_projections()[game])


def ats_to_wgs84(x: float, z: float) -> tuple[float, float]:
    return projection_for_game("ats")(x, z)


def ets2_to_wgs84(x: float, z: float) -> tuple[float, float]:
    return projection_for_game("ets2")(x, z)


PROJECTIONS = {"ats": ats_to_wgs84, "ets2": ets2_to_wgs84}
