"""Solar reference times, extreme latitudes, and DST boundaries."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from familiar_desk.sun import elevation, sun_times


@pytest.mark.parametrize(
    "latitude,longitude,zone,day,rise,set_,dawn,dusk",
    [
        (
            19.4326,
            -99.1332,
            "America/Mexico_City",
            "2026-11-20",
            "06:47",
            "17:57",
            "06:24",
            "18:20",
        ),
        (69.6492, 18.9553, "Europe/Oslo", "2026-03-20", "05:44", "18:02", "04:44", "19:02"),
        (40.7128, -74.006, "America/New_York", "2026-03-08", "07:19", "18:55", "06:52", "19:23"),
        (40.7128, -74.006, "America/New_York", "2026-11-01", "06:26", "16:51", "05:57", "17:20"),
    ],
)
def test_reference_times(latitude, longitude, zone, day, rise, set_, dawn, dusk):
    local_zone = ZoneInfo(zone)
    times = sun_times(latitude, longitude, date.fromisoformat(day), local_zone)
    for actual, expected in zip(
        (times.sunrise, times.sunset, times.civil_dawn, times.civil_dusk),
        (rise, set_, dawn, dusk),
        strict=True,
    ):
        target = datetime.fromisoformat(f"{day}T{expected}").replace(tzinfo=local_zone)
        assert abs((actual - target).total_seconds()) < 120
    assert not times.polar_day and not times.polar_night


@pytest.mark.parametrize("month,daylight", [(6, True), (12, False)])
def test_tromso_polar(month, daylight):
    times = sun_times(69.6492, 18.9553, date(2026, month, 21), ZoneInfo("Europe/Oslo"))
    assert times.sunrise is None and times.sunset is None
    assert times.polar_day is daylight
    assert times.polar_night is not daylight
    if not daylight:
        assert times.civil_dawn is not None and times.civil_dusk is not None


def test_position_requires_aware_time():
    with pytest.raises(ValueError, match="aware"):
        elevation(0, 0, datetime(2026, 1, 1))


def test_position_same_instant_in_different_zones():
    now = datetime(2026, 3, 8, 12, tzinfo=ZoneInfo("UTC"))
    assert elevation(40, -74, now) == elevation(
        40, -74, now.astimezone(ZoneInfo("America/New_York"))
    )
    assert elevation(0, 0, now) > elevation(0, 0, now + timedelta(hours=12))
