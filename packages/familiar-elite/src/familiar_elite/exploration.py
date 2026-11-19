"""Nearby interests and density-adjusted hints, never claims of unexplored space."""

import math

from familiar.interfaces import ActionSpec, ConfigError, load_data, spoken

from .edsm import EdsmClient, positive

ACTION_BUDGET_S = 19.0  # Leave time for returning a result inside the core's 20 s limit.


# Only applied at the tool boundary, after numeric filtering and ranking.
RESULT_QUANTITIES = {
    "distance_ly": ("distance", "ly"),
    "radius_ly": ("radius", "ly"),
    "distance_ls": ("distance_to_arrival", "ls"),
    # Ring measurements copied from EDSM and local journal scans, respectively.
    "massMT": ("mass", "Mt"),
    "innerRadius": ("inner_radius", "km"),
    "outerRadius": ("outer_radius", "km"),
    "MassMT": ("mass", "Mt"),
    "InnerRad": ("inner_radius", "m"),
    "OuterRad": ("outer_radius", "m"),
    "distance_from_sol_ly": ("distance_from_sol", "ly"),
    "height_off_plane_ly": ("height_off_plane", "ly"),
    "distance_from_sol_colonia_line_ly": ("distance_from_sol_colonia_line", "ly"),
    "distance_from_sol_sagittarius_a_line_ly": ("distance_from_sol_sagittarius_a_line", "ly"),
}


def spoken_result(value):
    """Copy a completed result, giving dimensional quantities explicit units."""
    if isinstance(value, list):
        return [spoken_result(item) for item in value]
    if not isinstance(value, dict):
        return value
    return dict(spoken_field(key, item) for key, item in value.items())


def spoken_field(key, value):
    if key in RESULT_QUANTITIES:
        name, unit = RESULT_QUANTITIES[key]
        return name, spoken(value, unit)
    if key == "coordinates":
        return key, [spoken(axis, "ly") for axis in value]
    return key, spoken_result(value)


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
                    "service": {"type": "string", "minLength": 1},
                },
                "additionalProperties": False,
            },
            timeout_s=20,
            read_only=True,
        ),
        "system_info": ActionSpec(
            "Read current system bodies, stations and services; EDSM reports plus our own scans",
            {
                "type": "object",
                "properties": {
                    "focus": {"type": "string", "enum": ["any", "bodies", "stations", "services"]},
                },
                "additionalProperties": False,
            },
            timeout_s=20,
            read_only=True,
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
            read_only=True,
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
    return {
        "name": station.get("name"),
        "type": station.get("type"),
        "largest_pad": pad,
        "distance_ls": station.get("distanceToArrival"),
        "economy": station.get("economy"),
        "body": station.get("body"),
        **{key: station[key] for key in interest["service_flags"].values() if key in station},
        "otherServices": station.get("otherServices", []),
    }


def service_matches(station, service, interest):
    if service is None:
        return True
    needle = service.strip().casefold()
    flag = interest["service_flags"].get(needle)
    return bool(flag and station.get(flag)) or any(
        needle in name.casefold() for name in station.get("otherServices", [])
    )


def body_summary(body, interest):
    return {
        "name": body.get("name"),
        "kind": body.get("subType"),
        "notable": notable(body, interest),
        "landable": body.get("isLandable"),
        "rings": body.get("rings", []),
        "distance_ls": body.get("distanceToArrival"),
    }


def local_body(scan, interest):
    kind = scan.get("PlanetClass", scan.get("StarType"))
    return {
        **body_summary(
            {
                "name": scan.get("BodyName"),
                "subType": interest["journal_body_classes"].get(kind, kind),
                "isLandable": scan.get("Landable"),
                "rings": scan.get("Rings", []),
                "atmosphereType": scan.get("Atmosphere"),
            },
            interest,
        ),
        **{key: scan[key] for key in ("WasDiscovered", "WasMapped") if key in scan},
        "first_discovery": scan.get("WasDiscovered") is False,
        "source": "our scans",
    }


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
        "stations": pads,
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
        deadline = self.client.clock() + ACTION_BUDGET_S
        try:
            if name == "system_info":
                return spoken_result(await self.system_info(arguments, state, deadline))
            if not state.system or not valid_position(state.star_pos):
                return {
                    "status": "unavailable",
                    "detail": "A known system and position from Location/FSDJump are required",
                }
            position = list(state.star_pos)
            if name == "nearby_systems":
                return spoken_result(
                    await self.nearby(
                        arguments, position, state.system, state.max_jump_range, deadline
                    )
                )
            return spoken_result(await self.hint(arguments, position, deadline))
        except (ConfigError, TypeError, ValueError, KeyError) as exc:
            return {"status": "error", "detail": str(exc)}

    async def system_info(self, arguments, state, deadline):
        focus = arguments.get("focus", "any")
        if arguments.keys() - {"focus"} or focus not in {"any", "bodies", "stations", "services"}:
            raise ValueError("focus must be any/bodies/stations/services")
        if not state.system:
            return {"status": "unavailable", "detail": "Current system is unknown"}
        interest = load_data("familiar_elite", "interest").value
        result = {
            "status": "done",
            "system": state.system,
            "offline": False,
            "local": self._local_system(state, interest),
            "edsm": {},
        }
        if focus in {"any", "bodies"}:
            reply = await self.client.fetch("bodies", {"systemName": state.system}, deadline)
            bodies = items(reply.data, "bodies")
            result["offline"] |= reply.offline
            result["edsm"].update(
                body_count=len(bodies),
                bodies=[body_summary(body, interest) for body in bodies],
                bodies_available=reply.data is not None,
            )
        if focus in {"any", "stations", "services"}:
            reply = await self.client.fetch("stations", {"systemName": state.system}, deadline)
            stations = items(reply.data, "stations")
            result["offline"] |= reply.offline
            transient = interest["transient_station_types"]
            result["edsm"].update(
                stations=[
                    station_summary(station, interest)
                    for station in stations
                    if station.get("type") not in transient
                ],
                station_count=sum(station.get("type") not in transient for station in stations),
                fleet_carriers=sum(station.get("type") in transient for station in stations),
                stations_available=reply.data is not None,
            )
        result["detail"] = (
            "EDSM reports are incomplete; local facts are our own journal/scans. "
            "Stations on a body indicate habitation; a body scan alone does not."
            + offline_suffix(result["offline"])
        )
        return result

    def _local_system(self, state, interest):
        local = {
            "source": "our journal/scans",
            "bodies": [local_body(scan, interest) for scan in state.scans.values()],
        }
        local.update(
            {
                key: getattr(state, key)
                for key in (
                    "population",
                    "government",
                    "allegiance",
                    "economy",
                    "security",
                    "faction",
                    "star_class",
                )
                if getattr(state, key) is not None
            }
        )
        # Keep the FSS total separate from the list of bodies actually scanned.
        local["fss_body_count"] = state.bodies
        if state.docked and state.station:
            local["station"] = {
                "name": state.station,
                "type": state.station_type,
                "economy": state.station_economy,
                "services": state.station_services,
            }
        return local

    def _radius(self, arguments, jump_range, limit):
        if arguments.get("radius_ly") is not None:
            return min(positive(arguments["radius_ly"], "radius_ly"), limit), "requested radius_ly"
        if valid_range(jump_range):
            return min(jump_range, limit), "Loadout MaxJumpRange"
        return min(self.default_range, limit), "default_jump_range_ly"

    async def nearby(self, arguments, position, current, jump_range, deadline):
        if arguments.keys() - {"focus", "count", "radius_ly", "service"}:
            raise ValueError("Unknown nearby_systems arguments")
        service = arguments.get("service")
        if service is not None and (not isinstance(service, str) or not service.strip()):
            raise ValueError("service must be a non-empty string")
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
        enriched = [
            await self._enrich(s, position, interest, deadline, service) for s in candidates[:6]
        ]
        ranked = sorted(
            [s for s, _ in enriched if service is None or s["stations"]],
            key=lambda s: (-score(s, weights), s["distance_ly"], s["name"]),
        )
        offline = reply.offline or any(failed for _, failed in enriched)
        return {
            "status": "done",
            "detail": (
                f"EDSM nearby systems; radius {spoken(radius, 'ly')} from {source} "
                f"(capped at {spoken(limit, 'ly')})"
            )
            + offline_suffix(offline),
            "radius_ly": radius,
            "radius_source": source,
            "offline": offline,
            "systems": [dict(s, reasons=reasons(s)) for s in ranked[:count]],
            "service": service,
            "coverage": "At most six shortlisted systems; missing reports do not exclude services.",
        }

    async def _enrich(self, system, position, interest, deadline, service=None):
        bodies = await self.client.fetch("bodies", {"systemName": system["name"]}, deadline)
        stations, failed = [], bodies.offline
        if (system.get("information") or {}).get("population", 0):
            reply = await self.client.fetch("stations", {"systemName": system["name"]}, deadline)
            stations, failed = items(reply.data, "stations"), failed or reply.offline
        stations = [station for station in stations if service_matches(station, service, interest)]
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
