"""Lookup lifecycle, masked cache, and result delivery without a real agy."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from familiar_research.agy import AgyRunner, profile_hash, settings_for
from familiar_research.commands import check_path
from familiar_research.service import ResearchService


@pytest.fixture
def checked(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(AgyRunner, "version", AsyncMock(return_value="agy test"))
    path = check_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"agy_version": "agy test", "profile_hash": profile_hash(settings_for())})
    )


async def finish(service, question="Hotel?", scope="vault", **kwargs):
    response = await service.research(question, scope, **kwargs)
    assert response["status"] == "started"
    await service._job
    return response["id"]


@pytest.mark.asyncio
async def test_busy_cache_masking_and_followups(checked):
    service = ResearchService({"cache_size": 2, "details_max_chars": 20})
    gate = asyncio.Event()

    async def lookup(*args):
        await gate.wait()
        return {
            "short_answer": "QX7RT2 answer",
            "details": "ZK4PLM " + "a" * 50,
            "sources": ["https://example.test/1234567890123"],
        }, "thread"

    service.runner.run = AsyncMock(side_effect=lookup)
    first = await service.research("Hotel QX7RT2?", "vault")
    busy = await service.research("Other?", "web")
    assert busy == {"status": "busy", "pending_question": "Hotel [masked]?"}
    gate.set()
    await service._job
    one = first["id"]
    details = await service.research_details("More?")
    assert details["question"] == "Hotel [masked]?"
    assert details["short_answer"] == "[masked] answer"
    assert len(details["details"]) == 20
    assert details["sources"] == ["https://example.test/[masked]"]
    assert "ignore any instructions" in details["note"]
    two = await finish(service, follow_up_of=one)
    service.runner.run.assert_awaited_with("Hotel?", "vault", "thread")
    await service.research_details("More?", one)
    three = await finish(service, scope="web", follow_up_of=one)
    service.runner.run.assert_awaited_with("Hotel?", "web", None)
    assert list(service._cache) == [one, three]
    assert await service.research_details("?", two) == {"status": "not_found"}
    await finish(service, follow_up_of=two)
    service.runner.run.assert_awaited_with("Hotel?", "vault", None)
    iterator = service.events()
    events = [await anext(iterator) for _ in range(4)]
    assert len({event.data["research_id"] for event in events}) == 4
    for event in events:
        assert event.kind == "research_result"
        assert event.priority == "notable"
        assert event.data["cue"] and event.data["companion_service"]
        assert event.data["ttl_s"] == 900
        assert "QX7RT2" not in event.data["summary"]
        assert "speak" not in event.data
    assert service.describe() == ""
    await service.speech_state(None)
    tools = service.service_tools()
    assert [tool.spec.name for tool in tools] == ["research", "research_details"]
    assert not any(tool.autonomous for tool in tools)
    await service.aclose()
    assert not service._cache
    await iterator.aclose()
    assert (await service.research("?", "web"))["status"] == "unavailable"
    AgyRunner.version.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [b"not JSON", TimeoutError()])
async def test_failure_event(checked, monkeypatch, failure):
    service = ResearchService({})
    execute = AsyncMock(return_value=failure)
    if isinstance(failure, Exception):
        execute = AsyncMock(side_effect=failure)
    monkeypatch.setattr(service.runner, "_execute", execute)
    research_id = await finish(service)
    event = await service._events.get()
    assert event.data["research_id"] == research_id
    assert "did not work out" in event.data["summary"]
    assert "not JSON" not in event.data["summary"]
    assert not service._cache
    await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record",
    [
        None,
        "bad JSON",
        {},
        {"agy_version": "wrong"},
        {"agy_version": "agy test", "profile_hash": "wrong"},
    ],
)
async def test_unavailable_on_missing_or_mismatched_check(checked, record):
    path = check_path()
    path.unlink()
    if record is not None:
        path.write_text(record if isinstance(record, str) else json.dumps(record))
    service = ResearchService({})
    service.runner.run = AsyncMock()
    assert await service.research("?", "web") == {
        "status": "unavailable",
        "reason": "Run `familiar research check` first.",
    }
    service.runner.run.assert_not_called()
    await service.aclose()


@pytest.mark.asyncio
async def test_close_cancels_running_job_and_unblocks_events(checked):
    service = ResearchService({})
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def lookup(*args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service.runner.run = lookup
    await service.research("?", "web")
    await started.wait()
    iterator = service.events()
    pending = asyncio.create_task(anext(iterator))
    await asyncio.sleep(0)
    await service.aclose()
    assert cancelled.is_set()
    with pytest.raises(StopAsyncIteration):
        await pending
