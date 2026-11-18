"""Nearby interests and density-adjusted hints, never claims of unexplored space."""

import math

from familiar.interfaces import ActionSpec, ConfigError, load_data

from .edsm import EdsmClient, positive

ACTION_BUDGET_S = 19.0  # Leave time for returning a result inside the core's 20 s limit.


def query_specs():
    return {
        "nearby_systems": ActionSpec(
            "Find nearby systems by interest using EDSM only when requested",
            {
                "type": "object",
                "properties": {
                    "focus": {
                        "type": "string",
                        "enum": ["any", "scenic", "inhabited", "exploration"],
                    },
                    "count": {"type": "integer", "minimum": 1, "maximum": 3},
                    "radius_ly": {"type": "number", "exclusiveMinimum": 0},
                },
                "additionalProperties": False,
            },
            timeout_s=20,
        ),
        "exploration_hint": ActionSpec(
            "Suggest least reported directions, not guaranteed unexplored, using EDSM on request",
            {
                "type": "object",
                "properties": {
                    "max_distance_ly": {"type": "number", "exclusiveMinimum": 0},
                },
                "additionalProperties": False,
            },
            timeout_s=20,
        ),
    }


def coordinates(system):
    value = system.get("coords", {})
    values = [value.get(axis) for axis in "xyz"]
    if not valid_position(values):
        return None
    return values


def valid_position(value):
    return (
        isinstance(value, (list, tuple))
        and len(value) == 3
        and all(type(v) in (int, float) and math.isfinite(v) for v in value)
    )


def items(payload, key):
    if isinstance(payload, list):
        return [entry for entry in payload if isinstance(entry, dict)]
    if isinstance(payload, dict):
        return items(payload.get(key), key)
    return []


def notable(body, interest):
    subtype = body.get("subType", "") or ""
    if subtype in interest["notable_planets"]:
        return subtype
    if any(name.casefold() in subtype.casefold() for name in interest["notable_stars"]):
        return subtype
    if body.get("rings") and interest["gas_giant_text"] in subtype.casefold():
        return "ringed gas giant"
    if body.get("isLandable") and body.get("atmosphereType") not in interest["no_atmospheres"] + [
        None
    ]:
        return "landable with atmosphere"
    return None


def primary(system):
    return system.get("primaryStar") or {}


def scoopable(system, interest):
    star = primary(system)
    if isinstance(star.get("isScoopable"), bool):
        return star["isScoopable"]
    return star.get("type", "").split(" ")[0] in interest["scoopable"]


def station_summary(station, interest):
    pad = station.get("maxLandingPadSize") or interest["station_pads"].get(station.get("type"))
    return {"name": station.get("name"), "type": station.get("type"), "largest_pad": pad}


def summarize(system, bodies, stations, position, interest):
    info = system.get("information") or {}
    star = {
        "name": primary(system).get("name", system["name"]),
        "subType": primary(system).get("type", ""),
    }
    features = bodies
    if not bodies and notable(star, interest):
        features = [star]
    featured = [
        {"name": body.get("name"), "kind": notable(body, interest)}
        for body in features
        if notable(body, interest)
    ]
    transient = set(interest["transient_station_types"])
    carriers = [station for station in stations if station.get("type") in transient]
    stations = [station for station in stations if station.get("type") not in transient]
    pads = [station_summary(station, interest) for station in stations]
    best = max(pads, key=lambda s: interest["pad_ranks"].get(s["largest_pad"], 0), default=None)
    inhabited = bool(info.get("population", 0))
    result = {
        "name": system["name"],
        "distance_ly": math.dist(position, coordinates(system)),
        "star_type": primary(system).get("type"),
        "scoopable": scoopable(system, interest),
        "station_count": len(stations),
        "fleet_carriers": len(carriers),
        "best_station": best,
        "notable_bodies": featured[:3],
        "body_count": len(bodies),
        "few_reports": not bodies and not info,
    }
    if inhabited:
        result.update(
            {key: info.get(key) for key in ("population", "economy", "allegiance", "security")}
        )
    return result


def score(summary, weights):
    signals = {
        "notable": len(summary["notable_bodies"]),
        "inhabited": int(bool(summary.get("population"))),
        "stations": summary["station_count"],
        "scoopable": int(summary["scoopable"]),
        "few_reports": int(summary["few_reports"]),
    }
    return sum(weights[key] * value for key, value in signals.items())


def reasons(summary):
    values = [body["kind"] for body in summary["notable_bodies"]]
    values += [
        text
        for enabled, text in (
            (summary.get("population"), "inhabited"),
            (summary["station_count"], "stations available"),
            (summary["scoopable"], "scoopable primary"),
            (summary["few_reports"], "few reports"),
        )
        if enabled
    ]
    return list(dict.fromkeys(values))


def line_distance(point, start, end):
    vector = [b - a for a, b in zip(start, end, strict=True)]
    offset = [p - a for a, p in zip(start, point, strict=True)]
    length_squared = sum(v * v for v in vector)
    fraction = sum(a * b for a, b in zip(offset, vector, strict=True)) / length_squared
    projection = [a + fraction * v for a, v in zip(start, vector, strict=True)]
    return math.dist(point, projection)


def offline_note(position, galaxy):
    return {
        "distance_from_sol_ly": math.dist(position, galaxy["sol"]),
        "height_off_plane_ly": abs(position[1]),
        "distance_from_sol_colonia_line_ly": line_distance(
            position, galaxy["sol"], galaxy["colonia"]
        ),
        "distance_from_sol_sagittarius_a_line_ly": line_distance(
            position, galaxy["sol"], galaxy["sagittarius_a"]
        ),
        "detail": (
            "Offline geometry: Sol–Colonia and Sol–Sagittarius A* are busy corridors; "
            "distance alone does not establish unexplored space."
        ),
    }


def density(position, galaxy):
    centre = galaxy["sagittarius_a"]
    radius = math.hypot(position[0] - centre[0], position[2] - centre[2])
    exponent = -abs(position[1]) / galaxy["scale_height_ly"] - radius / galaxy["scale_length_ly"]
    return math.exp(max(-700, exponent))


class Explorer:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client if client is not None else EdsmClient(settings)
        self.default_range = positive(
            settings.get("default_jump_range_ly", 20), "default_jump_range_ly"
        )

    async def perform(self, name, arguments, state):
        if not state.system or not valid_position(state.star_pos):
            return {
                "status": "unavailable",
                "detail": "A known system and position from Location/FSDJump are required",
            }
        position, system, jump_range = list(state.star_pos), state.system, state.max_jump_range
        deadline = self.client.clock() + ACTION_BUDGET_S
        try:
            if name == "nearby_systems":
                return await self.nearby(arguments, position, system, jump_range, deadline)
            return await self.hint(arguments, position, deadline)
        except (ConfigError, TypeError, ValueError, KeyError) as exc:
            return {"status": "error", "detail": str(exc)}

    def _radius(self, arguments, jump_range, limit):
        if arguments.get("radius_ly") is not None:
            return min(positive(arguments["radius_ly"], "radius_ly"), limit), "requested radius_ly"
        if valid_range(jump_range):
            return min(jump_range, limit), "Loadout MaxJumpRange"
        return min(self.default_range, limit), "default_jump_range_ly"

    async def nearby(self, arguments, position, current, jump_range, deadline):
        if arguments.keys() - {"focus", "count", "radius_ly"}:
            raise ValueError("Unknown nearby_systems arguments")
        focus, count = arguments.get("focus", "any"), arguments.get("count", 3)
        interest = load_data("familiar_elite", "interest").value
        if focus not in interest["weights"] or type(count) is not int or not 1 <= count <= 3:
            raise ValueError("focus must be any/scenic/inhabited/exploration; count must be 1..3")
        limit = load_data("familiar_elite", "edsm").value["endpoints"]["sphere"]["limit"]
        radius, source = self._radius(arguments, jump_range, limit)
        reply = await self.client.fetch(
            "sphere", dict(zip("xyz", position, strict=True), radius=radius), deadline
        )
        ignored = {name.casefold() for name in interest["ignored_systems"]}
        ignored.add(current.casefold())
        candidates = [
            s
            for s in items(reply.data, "systems")
            if s.get("name")
            and s["name"].casefold() not in ignored
            and coordinates(s) is not None
            and interest["min_distance_ly"] <= math.dist(position, coordinates(s)) <= radius
        ]
        weights = interest["weights"][focus]

        def cheap(s):
            return summarize(s, [], [], position, interest)

        candidates.sort(
            key=lambda s: (
                -score(cheap(s), weights),
                math.dist(position, coordinates(s)),
                s["name"],
            )
        )
        enriched = [await self._enrich(s, position, interest, deadline) for s in candidates[:6]]
        ranked = sorted(
            [s for s, _ in enriched],
            key=lambda s: (-score(s, weights), s["distance_ly"], s["name"]),
        )
        offline = reply.offline or any(failed for _, failed in enriched)
        return {
            "status": "done",
            "detail": (
                f"EDSM nearby systems; radius {radius:g} ly from {source} (capped at {limit:g} ly)"
            )
            + offline_suffix(offline),
            "radius_ly": radius,
            "radius_source": source,
            "offline": offline,
            "systems": [dict(s, reasons=reasons(s)) for s in ranked[:count]],
        }

    async def _enrich(self, system, position, interest, deadline):
        bodies = await self.client.fetch("bodies", {"systemName": system["name"]}, deadline)
        stations, failed = [], bodies.offline
        if (system.get("information") or {}).get("population", 0):
            reply = await self.client.fetch("stations", {"systemName": system["name"]}, deadline)
            stations, failed = items(reply.data, "stations"), failed or reply.offline
        summary = summarize(system, items(bodies.data, "bodies"), stations, position, interest)
        return summary, failed

    async def hint(self, arguments, position, deadline):
        if arguments.keys() - {"max_distance_ly"}:
            raise ValueError("Unknown exploration_hint arguments")
        galaxy = load_data("familiar_elite", "galaxy").value
        offset, size = galaxy["sample_offset_ly"], galaxy["sample_size_ly"]
        maximum = arguments.get("max_distance_ly")
        if maximum is not None:
            maximum = positive(maximum, "max_distance_ly")
            offset, size = min(offset, maximum / 2), min(size, maximum / math.sqrt(3))
        limit = load_data("familiar_elite", "edsm").value["endpoints"]["cube"]["limit"]
        samples = [
            await self._sample(direction, position, offset, min(size, limit), galaxy, deadline)
            for direction in galaxy["directions"].values()
        ]
        result = {
            "status": "done",
            "detail": "Least reported, not guaranteed unexplored",
            "offline_note": offline_note(position, galaxy),
        }
        if any(failed for _, failed in samples):
            return dict(
                result,
                detail=result["detail"] + "; offline: EDSM unavailable; offline note only",
                offline=True,
            )
        ranked = sorted([sample for sample, _ in samples], key=lambda s: s["report_ratio"])
        return dict(result, directions=ranked[:2], offline=False)

    async def _sample(self, direction, position, offset, size, galaxy, deadline):
        centre = list(position)
        centre[direction["axis"]] += direction["sign"] * offset
        reply = await self.client.fetch(
            "cube", dict(zip("xyz", centre, strict=True), size=size), deadline
        )
        systems = [
            s for s in items(reply.data, "systems") if s.get("name") and coordinates(s) is not None
        ]
        nearest = min(systems, key=lambda s: math.dist(coordinates(s), centre), default=None)
        waypoint = {"coordinates": centre}
        if nearest is not None:
            waypoint = {
                "name": nearest["name"],
                "distance_ly": math.dist(position, coordinates(nearest)),
            }
        expected = max(1e-300, density(centre, galaxy) * size**3)
        return {
            "direction": direction["words"],
            "reported_count": len(items(reply.data, "systems")),
            "report_ratio": len(items(reply.data, "systems")) / expected,
            "waypoint": waypoint,
        }, reply.offline or reply.data is None

    async def aclose(self):
        await self.client.aclose()


def valid_range(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def offline_suffix(offline):
    if offline:
        return "; offline: cached reports only, possibly incomplete"
    return ""
