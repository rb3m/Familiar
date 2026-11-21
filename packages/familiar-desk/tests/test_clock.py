"""Clock transitions and local solar context use supplied instants."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from familiar_desk.sources.clock import ClockSource


@pytest.mark.asyncio
async def test_fixed_period_events():
    source = ClockSource({"timezone": "UTC"})
    data, events = await source.sample(datetime(2026, 11, 20, 10, tzinfo=UTC))
    assert data["period"] == "morning" and events == []
    _, events = await source.sample(datetime(2026, 11, 20, 12, tzinfo=UTC))
    assert events[0]["id"] == "clock:period:2026-11-20:midday"
    assert events[0]["priority"] == "ambient"
    _, events = await source.sample(datetime(2026, 11, 20, 12, 1, tzinfo=UTC))
    assert events == []


@pytest.mark.asyncio
async def test_solar_twilight_and_sunset_notice():
    source = ClockSource(
        {"timezone": "America/Mexico_City", "latitude": 19.4326, "longitude": -99.1332}
    )
    zone = ZoneInfo("America/Mexico_City")
    data, _ = await source.sample(datetime(2026, 11, 20, 6, 30, tzinfo=zone))
    assert data["period"] == "dawn"
    assert data["sunrise"] and data["sunset"] and data["elevation"] < 0
    _, events = await source.sample(datetime(2026, 11, 20, 17, 30, tzinfo=zone))
    assert any(e["id"] == "clock:sunset_soon:2026-11-20" for e in events)
    _, events = await source.sample(datetime(2026, 11, 20, 17, 31, tzinfo=zone))
    assert not events
    data, _ = await source.sample(datetime(2026, 11, 20, 18, 10, tzinfo=zone))
    assert data["period"] == "dusk"


@pytest.mark.asyncio
async def test_dst_and_weekend():
    source = ClockSource({"timezone": "America/New_York"})
    before, _ = await source.sample(datetime(2026, 3, 8, 6, 59, tzinfo=UTC))
    after, _ = await source.sample(datetime(2026, 3, 8, 7, 1, tzinfo=UTC))
    assert before["local_time"].endswith("-05:00")
    assert after["local_time"].endswith("-04:00")
    assert after["weekday"] == "Sunday" and after["weekend"]
    assert after["timezone"] == "America/New_York"
