"""Weather providers, fallback, sparing remarks, and the prompt line."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from familiar.config import load_config
from familiar.interfaces import ConfigError
from familiar_desk.feed import Feed
from familiar_desk.service import DeskService
from familiar_desk.sources import load_sources
from familiar_desk.sources.weather import WeatherSource
from familiar_desk.weather import weather_settings

# 08:00 local in Querétaro (UTC-6).
NOW = datetime(2026, 11, 21, 14, tzinfo=UTC)
DESK = {
    "latitude": 20.59,
    "longitude": -100.39,
    "timezone": "America/Mexico_City",
    "location": "Querétaro",
}


def open_meteo(current_time="2026-11-21T08:00", code=1, hourly_codes=None, chance=0):
    hours = [f"2026-11-21T{hour:02d}:00" for hour in range(24)]
    codes = hourly_codes or [1] * 24
    return {
        "utc_offset_seconds": -21600,
        "current": {
            "time": current_time,
            "temperature_2m": 14.2,
            "apparent_temperature": 11.9,
            "relative_humidity_2m": 80,
            "cloud_cover": 22,
            "precipitation": 0.0,
            "weather_code": code,
            "wind_speed_10m": 1.0,
        },
        "hourly": {
            "time": hours,
            "temperature_2m": [10 + hour * 0.5 for hour in range(24)],
            "precipitation_probability": [chance] * 24,
            "weather_code": codes,
        },
        "daily": {
            "time": ["2026-11-21"],
            "temperature_2m_min": [11.7],
            "temperature_2m_max": [24.8],
            "precipitation_probability_max": [chance],
            "precipitation_sum": [0.0],
        },
    }


def metar(obs=NOW - timedelta(minutes=20), wx=None, lat=20.617, lon=-100.194):
    return [
        {
            "icaoId": "MMQT",
            "obsTime": int(obs.timestamp()),
            "temp": 12,
            "dewp": 6,
            "wspd": 10,
            "cover": "BKN",
            "wxString": wx,
            "lat": lat,
            "lon": lon,
        }
    ]


class Api:
    """Serves canned bodies; None means the service is down."""

    def __init__(self, om=None, mt=None):
        self.bodies = {"api.open-meteo.com": om, "aviationweather.gov": mt}
        self.hits = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.hits.append(request)
        body = self.bodies[request.url.host]
        if body is None:
            return httpx.Response(503)
        return httpx.Response(200, json=body)

    def source(self, **weather):
        settings = {**DESK, "weather": {"station": "MMQT", **weather}}
        return WeatherSource(settings, transport=httpx.MockTransport(self))


@pytest.mark.asyncio
async def test_open_meteo_current_and_remaining_windows():
    codes = [1] * 12 + [3] * 6 + [61] * 6
    api = Api(om=open_meteo(hourly_codes=codes, chance=60))
    data, _ = await api.source().sample(NOW)
    assert data["sources"] == ("open-meteo",)
    assert data["current"]["temperature_c"] == 14.2
    assert data["current"]["observed_at"] == "2026-11-21T08:00:00-06:00"
    assert data["today"]["condition"] == "rain"
    assert [w["name"] for w in data["today"]["windows"]] == ["morning", "afternoon", "evening"]
    assert data["today"]["windows"][0]["min_c"] == 14  # from 08:00, not 06:00
    assert data["today"]["windows"][2]["condition"] == "rain"
    request = api.hits[0]
    assert request.url.params["latitude"] == "20.59"
    assert request.url.params["timezone"] == "America/Mexico_City"
    assert request.headers["User-Agent"].startswith("Familiar/")
    assert len(api.hits) == 1  # forecast complete: METAR not needed


@pytest.mark.asyncio
async def test_unlikely_drizzle_reads_dry():
    api = Api(om=open_meteo(hourly_codes=[51] * 24, chance=10))
    data, _ = await api.source().sample(NOW)
    assert data["today"]["condition"] == "cloudy"


@pytest.mark.asyncio
async def test_metar_fills_in_when_open_meteo_is_down():
    api = Api(mt=metar(wx="-RA BR"))
    data, _ = await api.source().sample(NOW)
    assert data["sources"] == ("metar",)
    current = data["current"]
    assert current["condition"] == "rain"
    assert current["humidity_pct"] == 67
    assert current["cloud_cover_pct"] == 75
    assert current["wind_m_s"] == 5.1
    assert data["today"] is None
    assert data["errors"][0].startswith("open-meteo:")


@pytest.mark.asyncio
async def test_stale_forecast_current_is_replaced_but_forecast_kept():
    api = Api(om=open_meteo(current_time="2026-11-21T01:00"), mt=metar())
    data, _ = await api.source().sample(NOW)
    assert list(data["sources"]) == ["open-meteo", "metar"]
    assert data["current"]["cloud_cover_pct"] == 75
    assert data["today"]["max_c"] == 24.8
    assert "open-meteo: observation is stale" in data["errors"]


@pytest.mark.asyncio
async def test_far_station_is_refused_and_total_failure_raises():
    api = Api(mt=metar(lat=40.64, lon=-73.78))
    with pytest.raises(ValueError, match="km from the desk location"):
        await api.source().sample(NOW)


@pytest.mark.asyncio
async def test_failure_keeps_previous_data_in_feed(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    api = Api(om=open_meteo())
    source = api.source()
    feed = Feed([source], clock=lambda: NOW, path=tmp_path / "f.json", lock=tmp_path / "l")
    await feed.sample(source)
    api.bodies["api.open-meteo.com"] = None
    await feed.sample(source)
    entry = feed.envelope["sources"]["weather"]
    assert entry["status"] == "error"
    assert entry["data"]["current"]["temperature_c"] == 14.2


@pytest.mark.asyncio
async def test_remarks_are_sparing():
    api = Api(om=open_meteo())
    source = api.source(remark_every_h=3)
    clock = NOW
    _, events = await source.sample(clock)
    assert [e["kind"] for e in events] == ["weather:outlook"]
    assert "Querétaro" in events[0]["summary"]
    for _ in range(2):
        clock += timedelta(hours=1)
        _, events = await source.sample(clock)
        assert events == []  # no repeat outlook, report not due
    clock += timedelta(hours=1)
    _, events = await source.sample(clock)
    assert [e["kind"] for e in events] == ["weather:report"]
    assert "14 °C" in events[0]["summary"]
    assert events[0]["id"] == "weather:report:2026-11-21T11"


@pytest.mark.asyncio
async def test_rain_change_and_quiet_hours():
    api = Api(om=open_meteo(current_time="2026-11-21T12:00"))
    source = api.source(remark_every_h=0, outlook_hour=12, active_hours=[7, 22])
    clock = NOW + timedelta(hours=4)  # 12:00 local: past the morning outlook
    assert (await source.sample(clock))[1] == []
    api.bodies["api.open-meteo.com"] = open_meteo(current_time="2026-11-21T13:00", code=63)
    clock += timedelta(hours=1)
    _, events = await source.sample(clock)
    assert [e["summary"] for e in events] == ["It has started to rain in Querétaro."]
    api.bodies["api.open-meteo.com"] = open_meteo(current_time="2026-11-21T23:00", code=1)
    _, events = await source.sample(NOW + timedelta(hours=15))  # 23:00 local
    assert events == []


@pytest.mark.asyncio
async def test_weather_line_without_clock_and_staleness(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    api = Api(om=open_meteo())
    await Feed([api.source()], clock=lambda: NOW).run(asyncio.Event(), once=True)
    now = NOW
    settings = {**DESK, "sources": ["weather"], "weather": {}}
    service = DeskService(settings, clock=lambda: now)
    line = service.describe()
    assert line.startswith("Weather in Querétaro (background only")
    assert "14 °C, clear, feels like 12 °C" in line
    assert "low/high 12–25 °C" in line
    now += timedelta(hours=4)  # current too old; forecast still today
    line = service.describe()
    assert "14 °C" not in line and "Today clear" in line
    now += timedelta(days=1)
    assert service.describe() == ""


@pytest.mark.asyncio
async def test_forecast_date_uses_the_weather_place_timezone(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    body = open_meteo(current_time="2026-11-22T07:00")
    body["utc_offset_seconds"] = 32400
    body["daily"]["time"] = ["2026-11-22"]
    tokyo = {"timezone": "Asia/Tokyo", "location": "Tokyo", "providers": ["open-meteo"]}
    source = WeatherSource({**DESK, "weather": tokyo}, transport=httpx.MockTransport(Api(om=body)))
    clock = datetime(2026, 11, 21, 22, tzinfo=UTC)  # Nov 21 in Mexico, Nov 22 in Tokyo
    await Feed([source], clock=lambda: clock).run(asyncio.Event(), once=True)
    service = DeskService({**DESK, "sources": ["weather"], "weather": tokyo}, clock=lambda: clock)
    assert "Weather in Tokyo" in service.describe()
    assert "Today clear" in service.describe()


def test_settings_and_plugins_validate():
    assert weather_settings(None)["providers"] == ["open-meteo", "metar"]
    assert weather_settings(None)["active_hours"] == [0, 24]
    for bad in (
        {"remark_every_h": -1},
        {"active_hours": [22, 7]},
        {"providers": "metar"},
        {"station": " "},
        {"outlook_hour": 7.5},
    ):
        with pytest.raises(ConfigError):
            weather_settings(bad)
    with pytest.raises(ConfigError, match="Unknown weather provider"):
        WeatherSource({**DESK, "weather": {"providers": ["nope"]}})
    with pytest.raises(ConfigError, match="Unknown desk source"):
        load_sources({"sources": ["clock", "tides"]})
    assert [s.name for s in load_sources({**DESK, "sources": ["clock", "weather"]})] == [
        "clock",
        "weather",
    ]


@pytest.mark.parametrize("section", ['[desk]\nsources = "weather"', "[desk]\nweather = 3"])
def test_core_config_shapes(tmp_path, section):
    path = tmp_path / "desk.toml"
    path.write_text(section)
    with pytest.raises(ConfigError):
        load_config(path)


def test_feed_data_is_json_serialisable():
    api = Api(om=open_meteo())
    data, _ = asyncio.run(api.source().sample(NOW))
    assert json.loads(json.dumps(data))["today"]["windows"][0]["name"] == "morning"
