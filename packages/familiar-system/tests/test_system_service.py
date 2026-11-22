import asyncio
from datetime import UTC, datetime, timedelta
from threading import Event
from unittest.mock import Mock

import pytest
from familiar.interfaces import ConfigError
from familiar_system.readers import Filesystem, Load, Memory, Resources, Temperature
from familiar_system.service import Snapshot, SystemService


def reader():
    mock = Mock()
    mock.temperatures.return_value = [
        Temperature("cpu1", "coretemp", "Package id 0", "mockcpu", 50, 90),
        Temperature("cpu2", "coretemp", "Package id 1", "mockcpu2", 60, 90),
        Temperature("gpu", "amdgpu", "edge", "mockgpu", 55),
        Temperature("drive", "nvme", "Composite", "nvme7 (Cloudplum)", 65, 75),
        Temperature("hidden", "unknown", "board", "mockboard", 50),
    ]
    mock.resources.return_value = Resources(
        [Filesystem("/mock", "ext4", 1000, 80), Filesystem("/roomy", "ext4", 1000, 900)],
        Memory(1000, 200),
        Load(0.5, 0.4, 0.3, 8),
        500,
    )
    return mock


async def test_tools_snapshots_overview_and_units(settings):
    mock = reader()
    service = SystemService(settings, reader=mock)
    try:
        assert service.describe() == ""
        assert (await service.system_status("thermal"))["data"] == "unavailable"
        await service._sample("thermal")
        await service._sample("storage")
        overview = (await service.system_status())["data"]
        assert overview["cpu"]["value"] == 60
        assert overview["gpu"]["value"] == 55
        assert overview["drive"]["line_c"] == 75
        assert overview["drive"]["line_source"] == "device max"
        assert overview["filesystem"]["mountpoint"] == "/mock"
        assert overview["filesystem"]["free"]["unit"] == "bytes"
        assert overview["filesystem"]["free_pct"]["unit"] == "%"
        assert overview["uptime"]["unit"] == "s"
        thermal = await service.system_status("thermal")
        assert len(thermal["data"]) == 4
        assert "hidden" not in str(thermal)
        assert thermal["age_s"] < 1
        assert thermal["observed_at"].endswith("+00:00")
        assert thermal["stale"] is False
        assert (await service.system_status("memory"))["data"]["available"]["value"] == 200
        assert (await service.system_status("load"))["data"]["cpus"]["value"] == 8
        assert (await service.system_status("bad"))["status"] == "invalid_request"
        spec = service.service_tools()[0].spec
        assert spec.name == "system_status"
        # Lookups never invoke readers again.
        mock.temperatures.assert_called_once()
        mock.resources.assert_called_once()
    finally:
        await service.aclose()


async def test_stale_and_unavailable(settings):
    service = SystemService(settings, reader=reader())
    try:
        memory = (await service.system_status("memory"))["data"]
        assert memory["available"]["value"] == "unavailable"
        assert memory["available"]["stale"] is True
        await service._sample("thermal")
        old = service.snapshots["thermal"]
        service.snapshots["thermal"] = Snapshot(
            old.value, datetime.now(UTC) - timedelta(seconds=21)
        )
        result = await service.system_status("thermal")
        assert result["stale"] is True
        assert result["data"][0]["stale"] is True
        service.snapshots["thermal"] = Snapshot([], datetime.now(UTC))
        assert (await service.system_status("thermal"))["data"] == "unavailable"
    finally:
        await service.aclose()


async def test_events_start_two_tasks_and_close_cancels(settings):
    service = SystemService(settings, reader=reader())
    stream = service.events()
    pending = asyncio.create_task(anext(stream, None))
    try:
        await asyncio.wait_for(wait_for_samples(service), 1)
        assert len(service._tasks) == 2
        await service.aclose()
        assert await asyncio.wait_for(pending, 1) is None
        assert all(task.cancelled() for task in service._tasks)
    finally:
        await service.aclose()
        await stream.aclose()


async def wait_for_samples(service):
    while len(service.snapshots) < 2:
        await asyncio.sleep(0.001)


async def test_timeout_skips_hung_read_and_does_not_queue_another(settings):
    started, release = Event(), Event()
    mock = reader()

    def hung():
        started.set()
        release.wait(2)
        return []

    mock.temperatures.side_effect = hung
    service = SystemService({**settings, "read_timeout_s": 0.02}, reader=mock)
    try:
        await service._sample("thermal")
        assert started.is_set()
        assert "thermal" not in service.snapshots
        await service._sample("thermal")
        mock.temperatures.assert_called_once()
        await service._sample("storage")  # the other executor slot remains usable
        assert "storage" in service.snapshots
        await asyncio.wait_for(service.aclose(), 0.2)
    finally:
        release.set()
        await service.aclose()


async def test_service_publishes_one_folded_event(settings):
    mock = reader()
    service = SystemService(settings, reader=mock)
    try:
        await service._sample("thermal")
        mock.temperatures.return_value = [
            Temperature("cpu1", "coretemp", "Package id 0", "mockcpu", 95, 90),
            Temperature("drive", "nvme", "Composite", "nvme7 (Cloudplum)", 80, 75),
        ]
        await service._sample("thermal")
        assert service._events.empty()
        await service._sample("thermal")
        event = service._events.get_nowait()
        assert event.data["summary"].count("warning line") == 2
        assert service._events.empty()
    finally:
        await service.aclose()


@pytest.mark.parametrize("key,value", [("read_timeout_s", 0), ("thermal_interval_s", float("inf"))])
def test_service_rejects_invalid_intervals(settings, key, value):
    with pytest.raises(ConfigError, match=key):
        SystemService({**settings, key: value}, reader=reader())


async def test_director_feed_consumes_service_events(settings):
    from familiar.cli import _feed_service

    mock = reader()
    service = SystemService(settings, reader=mock)
    director = Mock()
    task = asyncio.create_task(_feed_service(service, director))
    try:
        await asyncio.wait_for(wait_for_samples(service), 1)
        mock.temperatures.return_value = [
            Temperature("cpu1", "coretemp", "Package id 0", "mockcpu", 95, 90),
        ]
        await service._sample("thermal")
        await service._sample("thermal")
        await asyncio.sleep(0)
        director.submit.assert_called_once()
        assert director.submit.call_args.args[0].kind == "system_notice"
    finally:
        await service.aclose()
        await asyncio.wait_for(task, 1)


async def test_failed_read_preserves_last_snapshot(settings):
    mock = reader()
    service = SystemService(settings, reader=mock)
    try:
        await service._sample("thermal")
        previous = service.snapshots["thermal"]
        mock.temperatures.side_effect = OSError("mock read failure")
        await service._sample("thermal")
        assert service.snapshots["thermal"] is previous
    finally:
        await service.aclose()
