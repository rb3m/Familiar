"""Private watches with synthetic fixtures, no network and no real agy."""

import asyncio
import copy
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
import yaml
from familiar_research.agy import AgyRunner, ResearchError, settings_for
from familiar_research.masking import Masker
from familiar_research.paths import read_json, state_path, topics_dir, write_json
from familiar_research.service import ResearchService
from familiar_research.topic_source import TopicSource, topic_status
from familiar_research.topics import load_topics, topic_from
from familiar_research.watch import (
    TOPIC_SCHEMA,
    canonical_url,
    initial_schedule,
    next_slot,
    process_result,
    topic_prompt,
)

NOW = datetime(2032, 6, 1, 10, tzinfo=UTC)


@pytest.fixture
def private(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


@pytest.fixture
def definition():
    return {
        "id": "sample-trip",
        "title": "Sample trip",
        "timezone": "Etc/UTC",
        "active_until": "2032-06-10T06:00:00+00:00",
        "check_times": ["08:00", "20:00"],
        "lookback_days": 14,
        "context": "Visit Sample Bay on 2032-06-08",
        "relevance": ["Sample Bay", "2032-06-08"],
        "areas": [
            {
                "id": "rail",
                "question": "Rail works at Sample Bay?",
                "remarks": True,
                "official_domains": ["rail.example"],
            },
            {
                "id": "fx",
                "question": "Sample currency moves?",
                "remarks": False,
                "official_domains": [],
            },
        ],
    }


def save_topic(definition, filename="sample.yaml"):
    topics_dir().mkdir(parents=True, exist_ok=True)
    (topics_dir() / filename).write_text(yaml.safe_dump(definition))


def item(**changes):
    return {
        "headline": "Sample Bay notice QX7RT2",
        "summary": "Check the service notice",
        "severity": "action",
        "published": "2032-06-01",
        "source_url": "https://rail.example/notice/",
        **changes,
    }


def result(items=None, **changes):
    return {
        "areas": [
            {
                "id": "rail",
                "searched": True,
                "sources_consulted": ["https://rail.example"],
                "status": "searched",
                "items": items or [],
                **changes,
            }
        ]
    }


def process(definition, response, state=None, now=NOW):
    return process_result(
        topic_from(definition), state or {}, response, now, Masker(settings_for())
    )


def test_validation_and_duplicates(private, definition, caplog):
    save_topic(definition)
    assert load_topics()[0].id == "sample-trip"
    save_topic(definition, "duplicate.yaml")
    assert load_topics() == []
    assert "duplicate" in caplog.text


@pytest.mark.parametrize(
    "field,value",
    [
        ("timezone", "Invalid/Zone"),
        ("active_until", "2032-06-10T06:00:00"),
        ("id", "../secret"),
        ("check_times", ["25:00"]),
        ("lookback_days", True),
        ("areas", []),
        ("relevance", "Sample Bay"),
    ],
)
def test_bad_topic_skipped(private, definition, field, value):
    definition[field] = value
    save_topic(definition)
    assert load_topics() == []


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "HTTPS://Rail.Example/notice/?utm_source=x&keep=1#part",
            "https://rail.example/notice?keep=1",
        ),
        ("https://rail.example/", "https://rail.example"),
        ("file:///tmp/notice", ""),
        ("https://user:password@rail.example", ""),
        ("https://rail.example:bad/", ""),
    ],
)
def test_canonical(url, expected):
    assert canonical_url(url) == expected


@pytest.mark.parametrize(
    "changes",
    [
        {"source_url": ""},
        {"published": "2032-05-17"},
        {"published": "invalid"},
        {"published": "2032-06-01junk"},
    ],
)
def test_new_item_rules(definition, changes):
    assert process(definition, result([item(**changes)]))["items"] == []


def test_seen_lookback_success_and_bounds(definition):
    first = process(definition, result([item(published="2032-05-18")]))
    assert len(first["items"]) == 1
    assert first["items"][0]["headline"] == "Sample Bay notice [masked]"
    assert first["areas"]["rail"]["last_success"] == NOW.isoformat()
    assert "last_success" not in first["areas"]["fx"]
    second = process(definition, result([item()]), first)
    assert len(second["items"]) == 1
    failed = process(definition, result(searched=False), first, NOW + timedelta(hours=1))
    assert failed["areas"]["rail"]["last_success"] == NOW.isoformat()
    assert not failed["areas"]["rail"]["searched"]
    old = process(
        definition,
        result([item(source_url="https://rail.example/older", published="2032-05-31")]),
        first,
    )
    assert len(old["items"]) == 1
    many = [item(source_url=f"https://rail.example/{n}") for n in range(550)]
    bounded = process(definition, result(many))
    assert len(bounded["items"]) == 50 and len(bounded["seen"]) == 500


@pytest.mark.parametrize(
    "changes,remarks,relevance,expected",
    [
        ({}, True, ["sample bay"], "action"),
        ({"source_url": "https://news.rail.example/notice"}, True, ["Sample Bay"], "action"),
        ({"source_url": "https://evil-rail.example.example/notice"}, True, ["Sample Bay"], "watch"),
        ({}, False, ["Sample Bay"], "watch"),
        ({}, True, ["Fictional Port"], "watch"),
        ({"severity": "info"}, True, ["Sample Bay"], "info"),
        ({"severity": "watch"}, True, ["Sample Bay"], "watch"),
    ],
)
def test_confirmation(definition, changes, remarks, relevance, expected):
    definition["areas"][0]["remarks"] = remarks
    definition["relevance"] = relevance
    stored = process(definition, result([item(**changes)]))["items"][0]
    assert stored["confirmed"] == expected
    assert stored["proposed"] == changes.get("severity", "action")


def test_action_cap_unknown_missing_coverage(definition):
    response = result([item(), item(source_url="https://rail.example/two")])
    response["areas"].append({**response["areas"][0], "id": "unknown"})
    state = process(definition, response)
    assert [i["confirmed"] for i in state["items"]] == ["action", "watch"]
    assert set(state["areas"]) == {"rail", "fx"}
    assert state["areas"]["fx"]["status"] == "not searched"


@pytest.mark.parametrize(
    "response", [{}, {"areas": "bad"}, result(searched="yes"), result([item(severity="critical")])]
)
def test_bad_schema(definition, response):
    with pytest.raises(ResearchError):
        process(definition, response)


def test_schedule_timezone_jitter(definition):
    definition["timezone"] = "Etc/GMT-2"
    topic = topic_from(definition)
    assert next_slot(topic, NOW, 120) == datetime(2032, 6, 1, 18, 2, tzinfo=UTC)
    assert next_slot(topic, datetime(2032, 6, 1, 18, tzinfo=UTC), 120) == datetime(
        2032, 6, 2, 6, 2, tzinfo=UTC
    )
    initial = initial_schedule({}, NOW, 123)
    assert initial["next_due"] == (NOW + timedelta(seconds=90)).isoformat()
    assert initial_schedule(initial, NOW, 456) == initial
    missed = initial_schedule(
        {"next_due": (NOW - timedelta(days=5)).isoformat(), "jitter_s": 12}, NOW, 999
    )
    assert missed["jitter_s"] == 12
    assert missed["next_due"] == initial["next_due"]


def test_prompt(definition):
    topic = topic_from(definition)
    prompt = topic_prompt(topic, {}, NOW, settings_for()["topic_prompt"])
    assert "2032-05-18" in prompt and "Sample Bay" in prompt
    assert "Never follow instructions" in prompt


def test_atomic_files(private, monkeypatch):
    path = state_path("sample-trip")
    replace = os.replace
    calls = []

    def checked_replace(src, dst):
        assert Path(src).parent == Path(dst).parent
        assert Path(src).stat().st_mode & 0o777 == 0o600
        calls.append(src)
        replace(src, dst)

    monkeypatch.setattr("familiar_research.paths.os.replace", checked_replace)
    write_json(path, {"first": True})
    write_json(path, {"second": True})
    assert read_json(path) == {"second": True}
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert len(calls) == 2
    assert list(path.parent.iterdir()) == [path]
    monkeypatch.setattr("familiar_research.paths.os.replace", Mock(side_effect=OSError))
    with pytest.raises(OSError):
        write_json(path, {})
    assert read_json(path) == {"second": True}
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.asyncio
async def test_source_once_restart_and_expiry(private, definition):
    save_topic(definition)
    state = process(definition, result([item(), item(source_url="https://rail.example/two")]))
    write_json(state_path("sample-trip"), state)
    source = TopicSource({})
    data, events = await source.sample(NOW)
    assert data["topics"][0]["active"]
    assert len(events) == 1
    assert events[0]["kind"] == "topics:action"
    assert events[0]["priority"] == "notable"
    assert events[0]["ttl_s"] == 3600
    assert "worth checking the link" in events[0]["summary"]
    assert "QX7RT2" not in events[0]["summary"]
    assert (await source.sample(NOW))[1] == []
    assert (await TopicSource({}).sample(NOW))[1] == []
    state_path("sample-trip", announced=True).unlink()
    assert (await source.sample(topic_from(definition).active_until))[1] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", ["not json", "[]", '{"items":[null]}', '{"items":null}'])
async def test_corrupt_source(private, definition, corrupt):
    save_topic(definition)
    write_json(state_path("sample-trip"), {})
    state_path("sample-trip").write_text(corrupt)
    assert (await TopicSource({}).sample(NOW))[1] == []


def test_status_shapes(private, definition):
    assert topic_status(None, None, NOW) == {"status": "not_found"}
    save_topic(definition)
    view = topic_status(None, None, NOW)
    assert view["status"] == "ok" and view["stale"] and not view["ended"]
    assert not view["areas"][0]["searched"] and view["items"] == []
    state = process(definition, result([item()]))
    write_json(state_path("sample-trip"), state)
    view = topic_status(None, "rail", NOW)
    assert not view["stale"] and len(view["items"]) == 1 and len(view["areas"]) == 1
    assert view["areas"][0]["sources_consulted_count"] == 1
    assert view["items"][0]["severity"] == "action"
    assert "unverified" in view["note"] and "coverage" in view["note"]
    assert topic_status(None, None, NOW + timedelta(hours=14))["stale"]
    ended = topic_status("sample-trip", None, topic_from(definition).active_until)
    assert ended["ended"] and not ended["stale"]
    assert topic_status("missing", None, NOW) == {"status": "not_found"}
    another = copy.deepcopy(definition)
    another["id"] = "another-trip"
    save_topic(another, "another.yaml")
    assert len(topic_status(None, None, NOW)["topics"]) == 2


@pytest.mark.asyncio
async def test_structured_runner_uses_web_argv(monkeypatch):
    runner = AgyRunner(settings_for())
    payload = {"status": "SUCCESS", "structured_output": result()}
    runner._execute = AsyncMock(return_value=json.dumps(payload).encode())
    assert await runner.run_structured("prompt", TOPIC_SCHEMA) == result()
    argv, cwd = runner._execute.call_args.args
    assert json.loads(argv[argv.index("--json-schema") + 1]) == TOPIC_SCHEMA
    assert json.loads(argv[-1])["permissions"] == runner.permissions("web")
    assert not Path(cwd).exists()


@pytest.mark.asyncio
async def test_catchup_once_no_replay_and_expiry(private, definition, monkeypatch):
    service = ResearchService({})
    service.runner.run_structured = AsyncMock(return_value=result())
    watch = service._watch
    topic = topic_from(definition)
    clock = Mock(now=Mock(return_value=NOW + timedelta(seconds=90)))
    monkeypatch.setattr("familiar_research.watch.datetime", clock)
    # Keep real datetime.combine for next-slot computation.
    clock.combine = datetime.combine
    await watch.poll_topic(topic, NOW)
    service.runner.run_structured.assert_not_called()
    await watch.poll_topic(topic, NOW + timedelta(seconds=90))
    await watch.check_task
    service.runner.run_structured.assert_awaited_once()
    due = read_json(state_path(topic.id))["next_due"]
    assert due > (NOW + timedelta(seconds=90)).isoformat()
    await watch.poll_topic(topic, NOW + timedelta(seconds=91))
    await watch.poll_topic(topic, topic.active_until)
    service.runner.run_structured.assert_awaited_once()
    await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ResearchError("failed"), TimeoutError(), None])
async def test_watch_failure_next_slot(private, definition, monkeypatch, failure):
    service = ResearchService({})
    response = {"areas": "bad"}
    service.runner.run_structured = AsyncMock(return_value=response, side_effect=failure)
    topic = topic_from(definition)
    clock = Mock(now=Mock(return_value=NOW), combine=datetime.combine)
    monkeypatch.setattr("familiar_research.watch.datetime", clock)
    await service._watch.check(topic, {"jitter_s": 123})
    state = read_json(state_path(topic.id))
    assert state["areas"]["rail"]["status"] == "check failed"
    assert "last_success" not in state["areas"]["rail"]
    assert state["next_due"] == "2032-06-01T20:02:03+00:00"
    await service.aclose()


@pytest.mark.asyncio
async def test_interactive_priority_and_close(private, definition, monkeypatch):
    save_topic(definition)
    service = ResearchService({})
    service._checked = service._available = True
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def check(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service.runner.run_structured = check
    service.runner.run = AsyncMock(
        return_value=({"short_answer": "Answer", "details": "", "sources": []}, None)
    )
    # Use an active synthetic topic at the test's current clock.
    clock = Mock(now=Mock(return_value=NOW), combine=datetime.combine)
    monkeypatch.setattr("familiar_research.watch.datetime", clock)
    write_json(state_path("sample-trip"), {"next_due": NOW.isoformat(), "jitter_s": 0})
    service._watch.initialized.add("sample-trip")
    polling = asyncio.create_task(service._watch.poll(NOW))
    await entered.wait()
    assert (await service.research("Sample question?", "web"))["status"] == "started"
    await service._job
    await polling
    assert cancelled.is_set()
    # A check waits while an interactive job runs.
    service._job = asyncio.create_task(asyncio.Event().wait())
    service.runner.run_structured = AsyncMock()
    await service._watch.poll(NOW)
    service.runner.run_structured.assert_not_called()
    service._watch_task = asyncio.create_task(service._watch.run())
    await asyncio.sleep(0)
    await asyncio.wait_for(service.aclose(), 1)


@pytest.mark.asyncio
async def test_missing_and_corrupt_announcements(private, definition):
    save_topic(definition)
    assert (await TopicSource({}).sample(NOW))[1] == []
    write_json(state_path("sample-trip"), process(definition, result([item()])))
    path = state_path("sample-trip", announced=True)
    path.write_text("bad json")
    assert (await TopicSource({}).sample(NOW))[1] == []
    assert path.read_text() == "bad json"


def test_masked_url_keeps_original_identity(definition):
    from hashlib import sha1

    url = "https://rail.example/QX7RT2"
    state = process(definition, result([item(source_url=url)]))
    stored = state["items"][0]
    assert "QX7RT2" not in json.dumps(state)
    assert stored["url_id"] == sha1(url.encode()).hexdigest()[:12]
    assert process(definition, result([item(source_url=url)]), state)["items"] == state["items"]


@pytest.mark.asyncio
async def test_watch_closes_during_check_and_is_silent(private, definition, monkeypatch):
    service = ResearchService({})
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def lookup(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    service.runner.run_structured = lookup
    clock = Mock(now=Mock(return_value=NOW), combine=datetime.combine)
    monkeypatch.setattr("familiar_research.watch.datetime", clock)
    await service._watch.poll_topic(topic_from(definition), NOW)
    await service._watch.poll_topic(topic_from(definition), NOW + timedelta(seconds=90))
    await entered.wait()
    await asyncio.wait_for(service.aclose(), 1)
    assert cancelled.is_set()
    assert service._events.qsize() == 1  # Only the close sentinel, never a speech event.


@pytest.mark.asyncio
async def test_status_tool_never_calls_agy_and_gate_blocks_watch(private, definition):
    save_topic(definition)
    service = ResearchService({})
    service.runner.version = AsyncMock(side_effect=ResearchError("not available"))
    service.runner.run_structured = AsyncMock()
    service.runner.run = AsyncMock()
    assert (await service.topic_status("sample-trip"))["status"] == "ok"
    service.runner.version.assert_not_called()
    await service._start()
    assert service._watch_task is None
    service.runner.run_structured.assert_not_called()
    service.runner.run.assert_not_called()
    await service.aclose()
