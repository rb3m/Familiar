"""Deterministic EDSM queries: no live network or player data."""

import math

import httpx
import pytest
from familiar.interfaces import load_data, spoken
from familiar_elite import adapter as adapter_module
from familiar_elite.adapter import EliteAdapter
from familiar_elite.edsm import EdsmClient
from familiar_elite.exploration import Explorer, density, offline_note, query_specs
from familiar_elite.paths import ElitePaths
from familiar_elite.state import EliteState


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.waits = []

    def __call__(self):
        return self.now

    async def sleep(self, delay):
        self.waits.append(delay)
        self.now += delay


@pytest.fixture
def client_factory(tmp_path):
    clients = []

    def make(handler, **settings):
        clock = Clock()
        client = EdsmClient(
            settings,
            transport=httpx.MockTransport(handler),
            clock=clock,
            sleeper=clock.sleep,
            cache_dir=tmp_path / "cache",
        )
        clients.append(client)
        return client, clock

    return make


def system(name, x=1, *, population=0, info=True, star="G (White-Yellow) Star"):
    entry = {"name": name, "coords": {"x": x, "y": 0, "z": 0}, "primaryStar": {"type": star}}
    if info:
        entry["information"] = {
            "population": population,
            "economy": "Industrial",
            "allegiance": "Independent",
            "security": "High",
        }
    return entry


def state():
    return EliteState(system="Sol", star_pos=[0, 0, 0], max_jump_range=30)


@pytest.mark.asyncio
async def test_cache_ttl_pacing_privacy_and_persistence(client_factory):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=[system("Alpha")])

    client, clock = client_factory(handler)
    params = {"radius": 20, "x": 0, "y": 0, "z": 0}
    first = await client.fetch("sphere", params, clock() + 20)
    assert first.data[0]["name"] == "Alpha"
    assert await client.fetch("sphere", dict(reversed(list(params.items()))), clock() + 20) == first
    assert len(calls) == 1
    # A separate instance shares the disk cache.
    second, _ = client_factory(handler)
    assert (await second.fetch("sphere", params, clock() + 20)).data == first.data
    assert len(calls) == 1
    await client.fetch("cube", {"x": 1, "y": 2, "z": 3, "size": 100}, clock() + 20)
    assert clock.waits[-1] == 1
    clock.now += 2592001
    await client.fetch("sphere", params, clock() + 20)
    assert len(calls) == 3
    assert calls[0].headers["User-Agent"].startswith("Familiar/")
    assert "http" not in calls[0].headers["User-Agent"]
    assert set(calls[0].url.params) == {
        "radius",
        "x",
        "y",
        "z",
        "showId",
        "showCoordinates",
        "showPrimaryStar",
        "showInformation",
    }
    await client.aclose()
    await second.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [429, 503])
async def test_retry_once_and_budget(client_factory, code):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(code, headers={"Retry-After": "3"})

    client, clock = client_factory(handler)
    reply = await client.fetch("bodies", {"systemName": "Alpha"}, clock() + 20)
    assert reply.offline and reply.data is None
    assert len(calls) == 2
    assert 3 in clock.waits
    calls.clear()
    reply = await client.fetch("bodies", {"systemName": "Alpha"}, clock() + 2)
    assert reply.offline
    assert not calls  # The second response also imposes a server cooldown.
    await client.aclose()


@pytest.mark.asyncio
async def test_offline_stale_cache_disabled_and_cache_ttls(client_factory):
    fail = False
    calls = []

    def handler(request):
        calls.append(request)
        if fail:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200, json={"bodies": [{"name": "Alpha 1"}]})

    client, clock = client_factory(handler)
    params = {"systemName": "Alpha"}
    await client.fetch("bodies", params, clock() + 20)
    clock.now += 604801
    fail = True
    reply = await client.fetch("bodies", params, clock() + 20)
    assert reply.offline and reply.data["bodies"][0]["name"] == "Alpha 1"
    client.enabled = False
    assert (await client.fetch("bodies", params, clock() + 20)).offline
    assert len(calls) == 2
    assert (await client.fetch("stations", params, clock() + 20)).data is None
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "focus,expected",
    [("any", "Scenic"), ("scenic", "Scenic"), ("inhabited", "Port"), ("exploration", "Sparse")],
)
async def test_focus_ranking_and_details(client_factory, focus, expected):
    sphere = [
        system("Sol", 0),
        system("Port", 2, population=100),
        system("Scenic", 3),
        system("Sparse", 4, info=False),
    ]
    calls = []
    bodies = [
        {"name": "Scenic 1", "subType": "Earthlike body"},
        {"name": "Scenic 2", "subType": "Water world"},
        {"name": "Scenic 3", "subType": "Class I gas giant", "rings": [{}]},
        {
            "name": "Scenic 4",
            "subType": "Rocky body",
            "isLandable": True,
            "atmosphereType": "Argon",
        },
    ]

    def handler(request):
        calls.append(request)
        match request.url.path:
            case "/api-v1/sphere-systems":
                return httpx.Response(200, json=sphere)
            case "/api-system-v1/bodies":
                name = request.url.params["systemName"]
                return httpx.Response(200, json={"bodies": bodies if name == "Scenic" else []})
            case "/api-system-v1/stations":
                return httpx.Response(
                    200,
                    json={
                        "stations": [
                            {"name": "Small", "type": "Outpost"},
                            {"name": "Big", "type": "Coriolis Starport"},
                        ]
                    },
                )
        raise AssertionError(request.url)

    client, _ = client_factory(handler)
    result = await Explorer({}, client).perform("nearby_systems", {"focus": focus}, state())
    assert result["systems"][0]["name"] == expected
    by_name = {s["name"]: s for s in result["systems"]}
    assert by_name["Port"]["station_count"] == 2
    assert by_name["Port"]["best_station"]["name"] == "Big"
    assert by_name["Port"]["population"] == 100
    assert by_name["Scenic"]["distance"] == "3 light-years"
    assert by_name["Scenic"]["body_count"] == 4
    assert len(by_name["Scenic"]["notable_bodies"]) == 3
    assert by_name["Scenic"]["scoopable"]
    assert "few reports" in by_name["Sparse"]["reasons"]
    assert len([r for r in calls if r.url.path.endswith("sphere-systems")]) == 1
    assert all(r.url.params.get("systemName") != "Sol" for r in calls)
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "jump,arguments,settings,radius,source",
    [
        (35, {}, {}, 35, "Loadout MaxJumpRange"),
        (None, {}, {}, 20, "default_jump_range_ly"),
        (None, {}, {"default_jump_range_ly": 40}, 40, "default_jump_range_ly"),
        (500, {}, {}, 100, "Loadout MaxJumpRange"),
        (30, {"radius_ly": 200}, {}, 100, "requested radius_ly"),
    ],
)
async def test_jump_range_fallback_cap(client_factory, jump, arguments, settings, radius, source):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[])

    client, _ = client_factory(handler)
    current = state()
    current.max_jump_range = jump
    result = await Explorer(settings, client).perform("nearby_systems", arguments, current)
    assert result["radius"] == spoken(radius, "ly")
    assert result["radius_source"] == source
    assert f"radius {spoken(radius, 'ly')}" in result["detail"]
    assert float(requests[0].url.params["radius"]) == radius
    await client.aclose()


@pytest.mark.asyncio
async def test_top_six_only_and_nearby_offline_cache(client_factory):
    calls = []
    fail = False

    def handler(request):
        calls.append(request)
        if fail:
            raise httpx.ConnectError("offline", request=request)
        if request.url.path.endswith("sphere-systems"):
            return httpx.Response(200, json=[system(str(i), i) for i in range(1, 12)])
        return httpx.Response(200, json={"bodies": []})

    client, clock = client_factory(handler)
    explorer = Explorer({}, client)
    await explorer.perform("nearby_systems", {}, state())
    assert len(calls) == 7
    fail = True
    clock.now += 2592001
    result = await explorer.perform("nearby_systems", {}, state())
    assert result["offline"] and "offline" in result["detail"]
    assert len(result["systems"]) == 3
    await client.aclose()


@pytest.mark.asyncio
async def test_direction_density_normalisation_waypoint_and_offline(client_factory):
    fail = False
    requests = []

    def handler(request):
        requests.append(request)
        if fail:
            return httpx.Response(503, headers={"Retry-After": "100"})
        centre = [float(request.url.params[axis]) for axis in "xyz"]
        # Equal counts: the denser directions should have the smallest ratio.
        entries = [
            {"name": "Far", "coords": dict(zip("xyz", [v + 20 for v in centre], strict=True))},
            {"name": "Centre", "coords": dict(zip("xyz", centre, strict=True))},
        ]
        return httpx.Response(200, json=entries)

    client, clock = client_factory(handler)
    explorer = Explorer({}, client)
    result = await explorer.perform("exploration_hint", {}, state())
    assert len(requests) == 6
    assert result["directions"][0]["direction"] == "coreward"
    assert result["directions"][0]["waypoint"]["name"] == "Centre"
    assert result["directions"][0]["waypoint"]["distance"] == "150 light-years"
    galaxy = load_data("familiar_elite", "galaxy").value
    assert result["directions"][0]["report_ratio"] == pytest.approx(
        2 / (density([0, 0, 150], galaxy) * 100**3)
    )
    assert "not guaranteed unexplored" in result["detail"]
    fail = True
    clock.now += 2592001
    result = await explorer.perform("exploration_hint", {}, state())
    assert result["offline"] and "offline" in result["detail"]
    assert "directions" not in result
    assert result["offline_note"]["distance_from_sol"] == "0 light-years"
    await client.aclose()


@pytest.mark.asyncio
async def test_empty_cube_waypoint_maximum_and_disabled_no_network(client_factory):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=[])

    client, _ = client_factory(handler)
    result = await Explorer({}, client).perform(
        "exploration_hint", {"max_distance_ly": 40}, state()
    )
    assert result["directions"][0]["waypoint"]["coordinates"] == [
        "20 light-years",
        "0 light-years",
        "0 light-years",
    ]
    assert all(float(r.url.params["size"]) <= 40 / math.sqrt(3) for r in calls)
    client.enabled = False
    calls.clear()
    result = await Explorer({}, client).perform("exploration_hint", {}, state())
    assert result["offline"] and not calls
    await client.aclose()


def test_offline_geometry_and_loadout():
    galaxy = load_data("familiar_elite", "galaxy").value
    note = offline_note(galaxy["colonia"], galaxy)
    assert note["distance_from_sol_colonia_line_ly"] == pytest.approx(0, abs=1e-8)
    assert note["height_off_plane_ly"] == 910.28125
    current = state()
    current.apply({"event": "Loadout", "MaxJumpRange": 45.5})
    assert current.max_jump_range == 45.5


@pytest.mark.asyncio
async def test_adapter_queries_no_keyboard_or_status_and_no_position(monkeypatch, client_factory):
    monkeypatch.setattr(adapter_module, "find_paths", lambda settings: ElitePaths())
    adapter = EliteAdapter({})
    client, _ = client_factory(lambda request: httpx.Response(200, json=[]))
    adapter._explorer = Explorer({}, client)
    assert (await adapter.perform("nearby_systems", {}))["status"] == "unavailable"
    adapter.state.apply({"event": "Location", "StarSystem": "Sol", "StarPos": [0, 0, 0]})
    assert adapter._keyboard is None
    assert not adapter.status.fresh()
    assert (await adapter.perform("nearby_systems", {}))["status"] == "done"
    assert (await adapter.perform("exploration_hint", {}))["status"] == "done"
    assert adapter.actions()["request_docking"].timeout_s == 15
    assert all(s.timeout_s == 20 for s in query_specs().values())
    assert all(spec.read_only for spec in query_specs().values())
    assert not adapter.actions()["request_docking"].read_only
    await adapter.aclose()


@pytest.mark.asyncio
async def test_retry_success_http_date_and_concurrent_pacing(client_factory):
    import asyncio
    from email.utils import formatdate

    times = []

    def handler(request):
        times.append(clock())
        if len(times) == 1:
            return httpx.Response(
                429, headers={"Retry-After": formatdate(clock() + 4, usegmt=True)}
            )
        return httpx.Response(200, json={"bodies": []})

    client, clock = client_factory(handler)
    replies = await asyncio.gather(
        client.fetch("bodies", {"systemName": "A"}, clock() + 20),
        client.fetch("bodies", {"systemName": "B"}, clock() + 20),
    )
    assert all(not reply.offline for reply in replies)
    assert times == [1000, 1004, 1005]
    await client.aclose()


@pytest.mark.asyncio
async def test_station_cache_one_day_ttl(client_factory):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"stations": []})

    client, clock = client_factory(handler)
    await client.fetch("stations", {"systemName": "A"}, clock() + 20)
    clock.now += 86399
    await client.fetch("stations", {"systemName": "A"}, clock() + 20)
    assert len(calls) == 1
    clock.now += 2
    await client.fetch("stations", {"systemName": "A"}, clock() + 20)
    assert len(calls) == 2
    await client.aclose()


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"subType": "Ammonia world"}, "Ammonia world"),
        ({"subType": "Black Hole"}, "Black Hole"),
        ({"subType": "Neutron Star"}, "Neutron Star"),
        ({"subType": "White Dwarf (DA) Star"}, "White Dwarf (DA) Star"),
        ({"subType": "Wolf-Rayet Star"}, "Wolf-Rayet Star"),
        (
            {"subType": "Rocky body", "isLandable": True, "atmosphereType": "Argon"},
            "landable with atmosphere",
        ),
        ({"subType": "Rocky body", "isLandable": True, "atmosphereType": "No atmosphere"}, None),
    ],
)
def test_notable_classes(body, expected):
    from familiar_elite.exploration import notable

    assert notable(body, load_data("familiar_elite", "interest").value) == expected


@pytest.mark.asyncio
async def test_sparse_candidate_survives_cheap_shortlist(client_factory):
    names = []

    def handler(request):
        if request.url.path.endswith("sphere-systems"):
            candidates = [system(str(i), i) for i in range(1, 9)] + [
                system("Sparse", 10, info=False)
            ]
            return httpx.Response(200, json=candidates)
        names.append(request.url.params["systemName"])
        return httpx.Response(200, json={"bodies": []})

    client, _ = client_factory(handler)
    result = await Explorer({}, client).perform("nearby_systems", {"focus": "exploration"}, state())
    assert result["systems"][0]["name"] == "Sparse"
    assert "Sparse" in names and len(names) == 6
    await client.aclose()


def test_carriers_never_best_station():
    from familiar.interfaces import load_data
    from familiar_elite.exploration import summarize

    interest = load_data("familiar_elite", "interest").value
    system = {"name": "Hub", "coords": {"x": 3, "y": 0, "z": 4}, "information": {"population": 9}}
    stations = [
        {"name": "XYZ-123", "type": "Fleet Carrier"},
        {"name": "Port", "type": "Outpost"},
    ]
    result = summarize(system, [], stations, [0, 0, 0], interest)
    assert result["best_station"]["name"] == "Port"
    assert result["station_count"] == 1 and result["fleet_carriers"] == 1


@pytest.mark.asyncio
async def test_system_info_reports_scans_services_and_offline_cache(client_factory):
    calls = []
    fail = False

    def handler(request):
        calls.append(request)
        if fail:
            raise httpx.ConnectError("offline", request=request)
        if request.url.path.endswith("/bodies"):
            return httpx.Response(
                200,
                json={
                    "bodies": [
                        {"name": "Sol 3", "subType": "Earthlike body", "distanceToArrival": 500},
                        {
                            "name": "Sol 4",
                            "subType": "Rocky body",
                            "isLandable": True,
                            "rings": [{}],
                        },
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "stations": [
                    {
                        "name": "Port",
                        "type": "Outpost",
                        "distanceToArrival": 600,
                        "economy": "Industrial",
                        "haveMarket": True,
                        "haveShipyard": False,
                        "haveOutfitting": True,
                        "body": {"name": "Sol 4"},
                        "otherServices": ["Search and Rescue", "Interstellar Factors Contact"],
                    },
                    {"name": "XYZ-123", "type": "Fleet Carrier"},
                ]
            },
        )

    current = state()
    current.star_pos = None  # This query needs only the system name.
    current.bodies = 9
    current.apply(
        {
            "event": "Scan",
            "BodyName": "Sol 3",
            "PlanetClass": "Earthlike body",
            "WasDiscovered": False,
            "WasMapped": False,
        }
    )
    client, clock = client_factory(handler)
    explorer = Explorer({}, client)
    result = await explorer.perform("system_info", {}, current)
    assert result["status"] == "done" and not result["offline"]
    assert len(calls) == 2 and clock.waits == [0, 1]
    assert result["edsm"]["body_count"] == 2
    assert result["edsm"]["bodies"][0]["notable"] == "Earthlike body"
    assert result["edsm"]["bodies"][1]["landable"]
    assert result["edsm"]["bodies"][1]["rings"] == [{}]
    assert result["edsm"]["bodies"][0]["distance_to_arrival"] == "500 light-seconds"
    assert result["local"]["fss_body_count"] == 9
    assert result["local"]["bodies"][0]["first_discovery"]
    assert result["local"]["bodies"][0]["source"] == "our scans"
    assert "WasDiscovered" not in result["edsm"]["bodies"][0]
    station = result["edsm"]["stations"][0]
    assert station["distance_to_arrival"] == "600 light-seconds" and station["largest_pad"] == "M"
    assert station["body"] == {"name": "Sol 4"}
    assert station["haveMarket"] and not station["haveShipyard"] and station["haveOutfitting"]
    assert station["otherServices"] == ["Search and Rescue", "Interstellar Factors Contact"]
    assert result["edsm"]["fleet_carriers"] == 1 and result["edsm"]["station_count"] == 1
    await explorer.perform("system_info", {}, current)
    assert len(calls) == 2  # Both endpoints cached.
    clock.now += 604801
    fail = True
    cached = await explorer.perform("system_info", {}, current)
    assert cached["offline"] and "offline" in cached["detail"]
    assert cached["edsm"] == result["edsm"] and cached["local"] == result["local"]
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "focus,endpoints",
    [
        ("bodies", ["bodies"]),
        ("stations", ["stations"]),
        ("services", ["stations"]),
    ],
)
async def test_system_info_focus(client_factory, focus, endpoints):
    calls = []

    def handler(request):
        calls.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={})

    client, _ = client_factory(handler)
    result = await Explorer({}, client).perform("system_info", {"focus": focus}, state())
    assert result["status"] == "done" and calls == endpoints
    await client.aclose()


@pytest.mark.asyncio
async def test_system_info_local_only_disabled_and_validation(client_factory):
    def handler(request):
        raise AssertionError("No network allowed")

    client, _ = client_factory(handler, edsm_enabled=False)
    current = state()
    current.apply({"event": "Docked", "StationName": "Home", "StationServices": ["market"]})
    explorer = Explorer({}, client)
    result = await explorer.perform("system_info", {}, current)
    assert result["offline"] and not result["edsm"]["bodies_available"]
    assert not result["edsm"]["stations_available"]
    assert result["local"]["station"]["services"] == ["market"]
    assert (await explorer.perform("system_info", {"focus": "wrong"}, current))["status"] == "error"
    assert (await explorer.perform("system_info", {"extra": 1}, current))["status"] == "error"
    current.system = None
    assert (await explorer.perform("system_info", {}, current))["status"] == "unavailable"
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "service,expected",
    [
        ("sEaRcH aNd ReScUe", "Rescue"),
        ("MARKET", "Market"),
        ("shipyard", "Yard"),
        ("outfitting", "Outfitter"),
        ("Interstellar Factors", "Rescue"),
        ("missing", None),
    ],
)
async def test_nearby_service_filter_no_extra_fanout(client_factory, service, expected):
    calls = []
    stations = [
        {
            "name": "Rescue",
            "type": "Outpost",
            "otherServices": ["Search and Rescue", "Interstellar Factors Contact"],
        },
        {"name": "Market", "type": "Coriolis Starport", "haveMarket": True},
        {"name": "Yard", "type": "Outpost", "haveShipyard": True},
        {"name": "Outfitter", "type": "Outpost", "haveOutfitting": True},
    ]

    def handler(request):
        calls.append(request)
        match request.url.path:
            case "/api-v1/sphere-systems":
                return httpx.Response(
                    200, json=[system(str(n), n, population=10) for n in range(1, 10)]
                )
            case "/api-system-v1/bodies":
                return httpx.Response(200, json={"bodies": []})
            case "/api-system-v1/stations":
                return httpx.Response(200, json={"stations": stations})
        raise AssertionError(request.url)

    client, _ = client_factory(handler)
    result = await Explorer({}, client).perform("nearby_systems", {"service": service}, state())
    assert len(calls) == 13  # One sphere and the same six body/station pairs as before.
    assert {r.url.params.get("systemName") for r in calls[1:]} == set("123456")
    if expected is None:
        assert result["systems"] == []
        await client.aclose()
        return
    assert len(result["systems"]) == 3
    assert all(s["best_station"]["name"] == expected for s in result["systems"])
    assert all(s["station_count"] == 1 for s in result["systems"])
    assert result["systems"][0]["distance"] == "1 light-year"
    await client.aclose()


@pytest.mark.asyncio
async def test_distance_ranking_stays_numeric_until_result(client_factory):
    def handler(request):
        if request.url.path.endswith("sphere-systems"):
            return httpx.Response(200, json=[system("Far", 10), system("Near", 2)])
        return httpx.Response(200, json={"bodies": []})

    client, _ = client_factory(handler)
    result = await Explorer({}, client).perform("nearby_systems", {}, state())
    assert [entry["name"] for entry in result["systems"]] == ["Near", "Far"]
    assert [entry["distance"] for entry in result["systems"]] == ["2 light-years", "10 light-years"]
    assert all("distance_ly" not in entry for entry in result["systems"])
    await client.aclose()


@pytest.mark.parametrize(
    "ring,expected",
    [
        (
            {"massMT": 1.234, "innerRadius": 1234.5, "outerRadius": 2345.6},
            {
                "mass": "1.2 megatonnes",
                "inner_radius": "1,235 kilometres",
                "outer_radius": "2,346 kilometres",
            },
        ),
        (
            {"MassMT": 1, "InnerRad": 1234500, "OuterRad": 2345600},
            {
                "mass": "1 megatonne",
                "inner_radius": "1,234,500 metres",
                "outer_radius": "2,345,600 metres",
            },
        ),
    ],
)
def test_ring_quantities_at_result_boundary(ring, expected):
    from familiar_elite.exploration import spoken_result

    raw = {"bodies": [{"rings": [ring]}]}
    assert spoken_result(raw)["bodies"][0]["rings"] == [expected]
    assert all(isinstance(value, (int, float)) for value in ring.values())
