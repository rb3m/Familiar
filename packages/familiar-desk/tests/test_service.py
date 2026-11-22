"""Readers deduplicate remarks and own only their helper process."""

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from familiar_desk.feed import Feed
from familiar_desk.paths import feed_path
from familiar_desk.service import DeskService
from familiar_desk.sources.clock import ClockSource

NOW = datetime(2026, 11, 20, 18, 42, tzinfo=UTC)


@pytest.fixture(autouse=True)
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))


async def snapshot():
    feed = Feed([ClockSource({"timezone": "UTC"})], clock=lambda: NOW)
    await feed.run(asyncio.Event(), once=True)
    data = json.loads(feed_path().read_text())
    data["events"] = [
        {
            "id": key,
            "ts": (NOW + timedelta(seconds=offset)).isoformat(),
            "kind": "clock:period",
            "priority": "ambient",
            "summary": "It is evening.",
            "ttl_s": ttl,
        }
        for key, offset, ttl in (("valid", -10, 60), ("old", -61, 600), ("expired", -20, 10))
    ]
    feed_path().write_text(json.dumps(data))


@pytest.mark.asyncio
async def test_context_does_not_consume_events_and_stales():
    await snapshot()
    now = NOW
    service = DeskService({}, clock=lambda: now)
    assert service.describe() == "Local time: Friday 2026-11-20 18:42 (dusk)."
    events = service._read()
    assert len(events) == 1
    assert events[0].data == {"summary": "It is evening.", "ttl_s": 60, "companion_service": True}
    assert service._read() == []
    now += timedelta(seconds=181)
    assert service.describe() == ""
    feed_path().write_text("partial")
    assert service._read() == []


@pytest.mark.asyncio
async def test_child_spawn_and_close(monkeypatch):
    await snapshot()
    child = Mock(returncode=None, wait=AsyncMock())
    spawn = AsyncMock(return_value=child)
    monkeypatch.setattr("familiar_desk.service.asyncio.create_subprocess_exec", spawn)
    service = DeskService({"config_path": "/tmp/my config.toml"}, clock=lambda: NOW)
    assert not spawn.called
    iterator = service.events()
    assert (await anext(iterator)).kind == "clock:period"
    spawn.assert_awaited_once_with(
        sys.executable, "-m", "familiar", "desk-feed", "--config", "/tmp/my config.toml"
    )
    await service.aclose()
    await iterator.aclose()
    child.terminate.assert_called_once()
    child.kill.assert_not_called()


@pytest.mark.asyncio
async def test_existing_writer_is_not_spawned_or_stopped(monkeypatch):
    spawn = AsyncMock()
    monkeypatch.setattr("familiar_desk.service.asyncio.create_subprocess_exec", spawn)
    owner = Feed([])
    owner.acquire()
    try:
        service = DeskService({}, clock=lambda: NOW)
        await service._start()
        await service.aclose()
        spawn.assert_not_called()
    finally:
        owner.close()


@pytest.mark.asyncio
async def test_kill_after_terminate_timeout():
    service = DeskService({}, clock=lambda: NOW)
    child = Mock(returncode=None, wait=AsyncMock(side_effect=[TimeoutError, None]))
    service._child = child
    await service.aclose()
    child.terminate.assert_called_once()
    child.kill.assert_called_once()


@pytest.mark.asyncio
async def test_no_tools_or_speech_work():
    service = DeskService({}, clock=lambda: NOW)
    assert service.service_tools() == []
    assert service.describe() == ""
    await service.speech_state(Mock())


@pytest.mark.asyncio
async def test_context_names_the_location():
    source = ClockSource({"timezone": "UTC", "location": "Corregidora, Querétaro"})
    await Feed([source], clock=lambda: NOW).run(asyncio.Event(), once=True)
    service = DeskService({}, clock=lambda: NOW)
    assert service.describe() == (
        "Local time in Corregidora, Querétaro: Friday 2026-11-20 18:42 (dusk)."
    )


@pytest.mark.asyncio
async def test_context_adds_location_notes():
    await Feed([ClockSource({"timezone": "UTC"})], clock=lambda: NOW).run(
        asyncio.Event(), once=True
    )
    service = DeskService({"location_notes": "The Big Q."}, clock=lambda: NOW)
    assert service.describe() == "Local time: Friday 2026-11-20 18:42 (dusk).\nThe Big Q."


@pytest.mark.asyncio
async def test_scene_context_frame_topics_and_freshness():
    await snapshot()
    data = json.loads(feed_path().read_text())
    clock = data["sources"]["clock"]["data"]
    clock.update(location="Secret city", sunset=(NOW + timedelta(minutes=30)).isoformat())
    data["sources"]["weather"] = {
        "status": "ok",
        "data": {
            "location": "Secret city",
            "current": {
                "observed_at": NOW.isoformat(),
                "condition": "light_rain",
                "temperature_c": 15,
            },
        },
    }
    feed_path().write_text(json.dumps(data))
    now = NOW
    service = DeskService(
        {"sources": ["clock", "weather"], "location_notes": "The Big Q."}, clock=lambda: now
    )
    scene = service.scene_context()
    assert scene.frame == "dusk; light rain"
    topics = {topic.family: topic.text for topic in scene.topics}
    assert topics["time"] == service._clock_line()
    assert "sunset is at 19:12" in topics["time"]
    assert topics["weather"] == service._weather_line()
    assert topics["location"] == "The Big Q."
    assert "Secret city" not in scene.frame and not any(char.isdigit() for char in scene.frame)
    assert len(service._read()) == 1  # context did not consume events
    now += timedelta(seconds=service.weather["max_age_s"] + 181)
    assert service.scene_context().frame == ""
    assert [topic.family for topic in service.scene_context().topics] == ["location"]


@pytest.mark.asyncio
async def test_scene_context_empty_and_optional_location():
    service = DeskService({}, clock=lambda: NOW)
    assert service.scene_context().topics == ()
    service.settings["location_notes"] = "The Big Q."
    assert service.scene_context().topics[0].text == "The Big Q."
    service.settings.pop("location_notes")
    await snapshot()
    assert [topic.family for topic in service.scene_context().topics] == ["time"]
