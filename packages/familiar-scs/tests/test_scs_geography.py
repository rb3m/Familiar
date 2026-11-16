from math import cos, log, pi, radians, sin, tan
from pathlib import Path

import pytest
from familiar.interfaces import ConfigError
from familiar_scs.geo import (
    bearing,
    bearing_difference,
    compass_name,
    compass_point,
    haversine,
    true_heading,
)
from familiar_scs.knowledge import KnowledgePack
from familiar_scs.places import COUNTRY_NAMES, REGION_NAMES, STATE_NAMES, PlaceIndex
from familiar_scs.projection import LENGTH_OF_DEGREE, RADIUS_M, ats_to_wgs84, ets2_to_wgs84
from familiar_scs.units import Units

FIXTURE = Path(__file__).parent / "fixtures/scs_landmarks.yaml"


def test_ats_projection():
    assert ats_to_wgs84(-42104.4, -29011.4) == pytest.approx((43.7339, -106.3402), abs=0.0001)
    assert ats_to_wgs84(0, 0) == pytest.approx((39, -96))


def test_ets2_projection_live_chateau_thierry():
    # User's live ETS2 position near Château-Thierry, France.
    assert ets2_to_wgs84(-26253.693, 6106.096) == pytest.approx((49.04, 3.29), abs=0.05)


@pytest.mark.parametrize("x,z", [(12000, 10000), (-50000, -30000), (-31000, -7000)])
def test_ets2_forward_round_trip(x, z):
    lat, lon = map(radians, ets2_to_wgs84(x, z))
    p1, p2, p0, lon0 = map(radians, (37, 65, 50, 15))
    n = log(cos(p1) / cos(p2)) / log(tan(pi / 4 + p2 / 2) / tan(pi / 4 + p1 / 2))
    f = cos(p1) * tan(pi / 4 + p1 / 2) ** n / n
    rho0 = RADIUS_M * f / tan(pi / 4 + p0 / 2) ** n
    rho = RADIUS_M * f / tan(pi / 4 + lat / 2) ** n
    xx = rho * sin(n * (lon - lon0)) / (0.0001729241463 * LENGTH_OF_DEGREE)
    zz = (rho0 - rho * cos(n * (lon - lon0))) / (-0.000171570875 * LENGTH_OF_DEGREE)
    sx, sy = x // 4000, z // 4000
    if sx <= -8 and sy <= -2 and (sx, sy) != (-8, -2):
        xx, zz = xx / 0.75 + 31100 / 2, zz / 0.75 + 5500 / 2
    assert (xx + 16660, zz + 4150) == pytest.approx((x, z), abs=1e-7)


def test_geo_and_places(tmp_path):
    assert haversine(0, 0, 0, 1) == pytest.approx(111.1949, rel=1e-5)
    assert haversine(0, 0, 0, 0) == 0
    assert compass_point(0) == "north"
    assert compass_point(0.25) == "west"  # SDK: counterclockwise from north
    assert compass_point(0.5) == "south"
    assert compass_point(0.75) == "east"
    assert compass_point(1) == "north"
    assert bearing(0, 0, 1, 0) == 0
    assert bearing_difference(350, 10) == 20
    assert bearing_difference(10, 350) == -20
    path = tmp_path / "places.csv"
    path.write_text(
        "name,state,lat,lon,land_km2\nKaycee,WY,43.7,-106.6,20\nBoise,ID,43.6,-116.2,40\n"
    )
    index = PlaceIndex(path)
    nearest = index.nearest(43.71, -106.61)
    assert nearest.name == "Kaycee"
    assert 1 < nearest.distance_km < 2
    assert PlaceIndex(places=()).nearest(0, 0) is None
    assert len(STATE_NAMES) == 49
    assert STATE_NAMES["WY"] == "Wyoming"


def test_region_names_per_game():
    assert REGION_NAMES["ats"] is STATE_NAMES
    assert REGION_NAMES["ets2"] is COUNTRY_NAMES
    assert STATE_NAMES["DE"] == "Delaware"
    assert COUNTRY_NAMES["DE"] == "Germany"
    assert STATE_NAMES["AL"] == "Alabama"
    assert COUNTRY_NAMES["AL"] == "Albania"
    assert COUNTRY_NAMES["GB"] == "United Kingdom"
    assert COUNTRY_NAMES["CZ"] == "Czechia"
    assert COUNTRY_NAMES["MK"] == "North Macedonia"
    assert COUNTRY_NAMES["XK"] == "Kosovo"


@pytest.mark.parametrize(
    "header,row,area,population",
    [
        ("", "", 0, 0),
        (",land_km2,population", ",,", 0, 0),
        (",population", ",20000", 0, 20000),
        (",land_km2", ",20", 20, 0),
    ],
)
def test_optional_place_area_and_population(tmp_path, header, row, area, population):
    path = tmp_path / "places.csv"
    path.write_text(f"name,state,lat,lon{header}\nTown,FR,49,3{row}\n")
    place = PlaceIndex(path).nearest(49, 3)
    assert place.land_km2 == area
    assert place.population == population


def test_knowledge_pack():
    pack = KnowledgePack.load([FIXTURE])
    nearby = pack.nearby(43.7339, -106.3402)
    assert [(p.id, d) for p, d in nearby] == [("kaycee", 0)]
    assert len(pack.landmarks) == 3
    bundled = KnowledgePack.for_game("ats")
    assert len(bundled.landmarks) >= 100  # the verified ATS pack ships with the package
    assert {"morton-pumpkin-capital"} <= {landmark.id for landmark in bundled.landmarks}
    extended = KnowledgePack.for_game("ats", [str(FIXTURE)])
    assert len(extended.landmarks) == len(bundled.landmarks) + 3


@pytest.mark.parametrize(
    "replacement",
    [
        {"lat": 91},
        {"radius_km": -1},
        {"lon": "nan"},
        {"facts": "text"},
        {"source": "file:///tmp/x"},
        {"id": ""},
        {"highway": []},
        {"name": None},
    ],
)
def test_knowledge_invalid(tmp_path, replacement):
    import yaml

    entry = yaml.safe_load(FIXTURE.read_text())[0]
    entry.update(replacement)
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump([entry]))
    with pytest.raises(ConfigError, match="bad.yaml: landmark"):
        KnowledgePack.load([path])


@pytest.mark.parametrize("content", ["foo: bar", "- [", "- 3"])
def test_knowledge_invalid_structure(tmp_path, content):
    path = tmp_path / "bad.yaml"
    path.write_text(content)
    with pytest.raises(ConfigError, match="bad.yaml"):
        KnowledgePack.load([path])


def test_duplicate_knowledge():
    with pytest.raises(ConfigError, match="kaycee: duplicate id"):
        KnowledgePack.load([FIXTURE, FIXTURE])


def test_units():
    imperial, metric = Units(), Units("metric", "ets2")
    assert imperial.speed(100) == "62 miles per hour"
    assert imperial.distance(72.42) == "45 miles"
    assert imperial.volume(144) == "38 gallons"
    assert imperial.money(1234) == "$1,234"
    assert imperial.mass(9071.8474) == "20,000 pounds"
    assert metric.speed(100) == "100 kilometers per hour"
    assert metric.distance(72.42) == "72 kilometers"
    assert metric.volume(144) == "144 liters"
    assert metric.money(1234) == "€1,234"
    assert metric.mass(18000) == "18 tonnes"
    assert metric.mass(22500) == "22.5 tonnes" and metric.mass(1000) == "1 tonne"
    assert imperial.distance(1.609344) == "1 mile"
    with pytest.raises(ConfigError):
        Units("unknown")


def test_packaged_places_loaded_once(tmp_path, monkeypatch):
    from familiar_scs import places

    data = tmp_path / "data"
    data.mkdir()
    (data / "us_places.csv").write_text("name,state,lat,lon,land_km2\nKaycee,WY,43.7,-106.6,20\n")
    (data / "eu_places.csv").write_text(
        "name,state,lat,lon,population\nParis,FR,48.86,2.35,2000000\n"
    )
    calls = []

    def resources(package):
        calls.append(package)
        return tmp_path

    monkeypatch.setattr(places, "files", resources)
    places._packaged_places.cache_clear()
    try:
        assert PlaceIndex().nearest(43.7, -106.6).name == "Kaycee"
        assert PlaceIndex().nearest(43.7, -106.6).distance_km == 0
        europe = places._packaged_places("eu_places.csv")
        assert europe[0].name == "Paris"
        assert places._packaged_places("eu_places.csv") is europe
        assert PlaceIndex().nearest(43.7, -106.6).name == "Kaycee"
        assert calls == ["familiar_scs", "familiar_scs"]
    finally:
        places._packaged_places.cache_clear()


def test_missing_packaged_places(tmp_path, monkeypatch):
    from familiar_scs import places

    monkeypatch.setattr(places, "files", lambda package: tmp_path)
    places._packaged_places.cache_clear()
    with pytest.raises(ConfigError, match="Missing SCS gazetteer"):
        PlaceIndex()


@pytest.mark.parametrize(
    ("heading", "expected"), [(0, "north"), (0.25, "west"), (0.5, "south"), (0.75, "east")]
)
def test_true_heading_in_game(heading, expected):
    from familiar_scs.adapter import AtsAdapter

    projection = AtsAdapter({}).projection
    # Iowa, close to the projection's central meridian (-96).
    x, z = 8000.0, -15000.0
    assert compass_name(true_heading(projection, x, z, heading)) == expected


def test_true_heading_corrects_map_rotation_far_from_central_meridian():
    from familiar_scs.adapter import AtsAdapter

    projection = AtsAdapter({}).projection
    lat, lon = projection(-95000.0, -45000.0)
    assert lon < -115  # Pacific Northwest, far west of the -96 meridian
    # Meridians converge toward the cone's apex, so map "up" there is ~15 degrees west of true
    # north (convergence = n * (lon - lon_0) with n ~ 0.63).
    assert 330 < true_heading(projection, -95000.0, -45000.0, 0.0) < 352
