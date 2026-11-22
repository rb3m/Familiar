from datetime import UTC, datetime

from familiar_system.alerts import AlertTracker
from familiar_system.readers import Filesystem, Temperature
from familiar_system.sensors import MatchedTemperature


def thermal(value, id="mockdrive"):
    return MatchedTemperature(
        Temperature(id=id, device=id, celsius=value), "drive", 75, "device max"
    )


def sample(tracker, value, time, **kwargs):
    return tracker.sample(
        timestamp=datetime.fromtimestamp(time, UTC), temperatures=[thermal(value)], **kwargs
    )


def test_seed_hot_silently_dwell_clear_cooldown_and_recross(settings):
    tracker = AlertTracker(settings)
    assert sample(tracker, 80, 0) is None
    assert sample(tracker, 80, 10) is None
    assert sample(tracker, 71, 20) is None  # not yet clear
    assert sample(tracker, 80, 30) is None
    assert sample(tracker, 70, 40) is None
    assert sample(tracker, 76, 50) is None
    event = sample(tracker, 77, 60)
    assert event.kind == "system_notice"
    assert event.priority == "notable"
    assert event.data["companion_service"] is True
    assert event.data["ttl_s"] == 180
    assert "cue" not in event.data
    assert "77 °C" in event.data["summary"]
    assert "75 °C warning line" in event.data["summary"]
    assert "drive mockdrive is at" in event.data["summary"]  # spoken name, not driver ids
    assert sample(tracker, 70, 70) is None
    assert sample(tracker, 80, 80) is None
    assert sample(tracker, 80, 90) is None  # cooldown suppresses and latches
    assert sample(tracker, 80, 1000) is None
    assert sample(tracker, 70, 1010) is None
    assert sample(tracker, 80, 1020) is None
    assert sample(tracker, 80, 1030) is not None


def test_dwell_requires_consecutive_hot_readings(settings):
    tracker = AlertTracker(settings)
    assert sample(tracker, 60, 0) is None
    assert sample(tracker, 80, 10) is None
    assert sample(tracker, 74, 20) is None
    assert sample(tracker, 80, 30) is None
    assert sample(tracker, 80, 40) is not None


def test_stale_missing_neither_fire_nor_clear(settings):
    tracker = AlertTracker(settings)
    assert sample(tracker, 60, 0) is None
    assert sample(tracker, 80, 10, stale=True) is None
    assert sample(tracker, 80, 20) is None
    assert tracker.sample(timestamp=datetime.fromtimestamp(25, UTC)) is None
    assert sample(tracker, None, 26) is None
    assert sample(tracker, 80, 30) is not None
    assert sample(tracker, 60, 40, stale=True) is None
    assert sample(tracker, 80, 1000) is None


def test_storage_rearm_and_fold_all_crossings(settings):
    tracker = AlertTracker(settings)

    def tick(value, free, time):
        return tracker.sample(
            timestamp=datetime.fromtimestamp(time, UTC),
            temperatures=[thermal(value), thermal(value, "mockdrive2")],
            filesystems=[Filesystem("/mock", "ext4", 1000_000_000_000, free * 10_000_000_000)],
        )

    assert tick(60, 20, 0) is None
    assert tick(80, 8, 10) is None
    event = tick(80, 8, 20)
    assert event.data["summary"].count("warning line") == 2
    assert "/mock has 8% free (80 GB)" in event.data["summary"]
    assert tick(80, 11, 1000) is None
    assert tick(80, 8, 1010) is None
    assert tick(80, 12, 1020) is None
    assert tick(80, 8, 1030) is None
    assert tick(80, 8, 1040) is not None
