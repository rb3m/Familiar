"""Telemetry alerts: conversions, one-shot crossings, suspension and prediction."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from familiar.alerts import AlertKeeper
from familiar.config import load_config
from familiar.director import Director
from familiar.interfaces import ConfigError, GameEvent, ToolCall, WatchSample, WatchSpec
from familiar.tools import ToolRegistry


class Adapter:
    def __init__(self, value=0, unit="m"):
        self.unit = unit
        self.sample = WatchSample({"q": value}, "vessel", True, True, 0.0)
        self.reads = 0

    def watchables(self):
        return {"q": WatchSpec(self.unit, "test quantity")}

    def watch_sample(self):
        self.reads += 1
        return self.sample


def sample(value, timestamp=0.5, **changes):
    return WatchSample(
        {"q": value},
        changes.get("context", "vessel"),
        changes.get("valid", True),
        changes.get("predictable", True),
        timestamp,
    )


@pytest.mark.parametrize(
    "unit,si,factor",
    [
        ("m", "m", 1),
        ("km", "m", 1000),
        ("ft", "m", 0.3048),
        ("mi", "m", 1609.344),
        ("nmi", "m", 1852),
        ("m/s", "m/s", 1),
        ("km/h", "m/s", 1 / 3.6),
        ("mph", "m/s", 0.44704),
        ("kn", "m/s", 1852 / 3600),
        ("s", "s", 1),
        ("min", "s", 60),
        ("h", "s", 3600),
        ("%", "%", 1),
        ("count", "count", 1),
    ],
)
async def test_conversions_and_confirmation(unit, si, factor):
    keeper = AlertKeeper(Adapter(unit=si))
    result = await keeper.set_alert("q", "above", 25, unit, "watch this")
    assert result["status"] == "created"
    assert result["si_value"] == pytest.approx(25 * factor)
    assert (result["value"], result["unit"], result["si_unit"]) == (25, unit, si)
    assert result["label"] == "watch this"
    assert keeper.adapter.reads == 1


@pytest.mark.parametrize(
    "args",
    [
        ("unknown", "above", 1, "m"),
        ("q", "up", 1, "m"),
        ("q", "above", 1, "s"),
        ("q", "above", 1, "parsec"),
        ("q", "above", float("inf"), "m"),
        ("q", "above", True, "m"),
    ],
)
async def test_reject_bad_requests(args):
    keeper = AlertKeeper(Adapter())
    assert "error" in await keeper.set_alert(*args)
    assert not keeper.alerts


@pytest.mark.parametrize("value,valid", [(None, True), (float("nan"), True), (0, False)])
async def test_unavailable(value, valid):
    adapter = Adapter()
    adapter.sample = sample(value, valid=valid)
    keeper = AlertKeeper(adapter)
    assert "currently unavailable" in (await keeper.set_alert("q", "above", 10, "m"))["error"]


@pytest.mark.parametrize(
    "direction,current", [("above", 10), ("above", 11), ("below", 10), ("below", 9)]
)
async def test_already_satisfied(direction, current):
    keeper = AlertKeeper(Adapter(current))
    result = await keeper.set_alert("q", direction, 10, "m")
    assert result["status"] == "already_satisfied"
    assert result["current_value"] == current
    assert not keeper.alerts


@pytest.mark.parametrize(
    "direction,start,finish",
    [("above", 0, 10), ("above", 0, 20), ("below", 20, 10), ("below", 20, 0)],
)
async def test_actual_crossings_equality_and_jumps(direction, start, finish):
    keeper = AlertKeeper(Adapter(start), {"lead_s": 0})
    result = await keeper.set_alert("q", direction, 10, "m", "ten")
    events = keeper.observe(sample(finish))
    assert len(events) == 1
    event = events[0]
    assert (event.kind, event.priority) == ("alert", "critical")
    assert event.data["alert_id"] == result["alert_id"]
    assert event.data["ttl_s"] == 10
    assert event.data["memory_salience"] == 0
    assert "as the player asked" in event.data["summary"]
    assert "Label: ten" in event.data["facts"]
    assert keeper.observe(sample(finish, 1)) == []
    assert not keeper.alerts


async def test_noise_cannot_rearm_within_tiny_hysteresis():
    keeper = AlertKeeper(Adapter(10 - 1e-10), {"lead_s": 0})
    await keeper.set_alert("q", "above", 10, "m")
    for i, value in enumerate([10 + 1e-10, 10 - 1e-10, 10], 1):
        assert keeper.observe(sample(value, i * 0.5)) == []
    assert keeper.observe(sample(9, 2)) == []
    assert len(keeper.observe(sample(10, 2.5))) == 1


async def test_invalid_gap_and_missing_values_rebaseline():
    keeper = AlertKeeper(Adapter())
    await keeper.set_alert("q", "above", 10, "m")
    assert keeper.observe(sample(9, valid=False, context=None)) == []
    assert keeper.observe(sample(11, 1)) == []  # no crossing inferred across pause
    assert keeper.observe(sample(9, 1.5)) == []
    assert keeper.observe(sample(None, 2)) == []
    assert keeper.observe(sample(11, 2.5)) == []
    assert keeper.observe(sample(8, 3)) == []
    assert len(keeper.observe(sample(10, 3.5))) == 1


async def test_context_change_drops_and_logs(caplog):
    adapter = Adapter()
    keeper = AlertKeeper(adapter)
    result = await keeper.set_alert("q", "above", 10, "m")
    assert keeper.observe(sample(11, context="other vessel")) == []
    listing = await keeper.list_alerts()
    assert listing["alerts"] == []
    assert listing["history"][0]["status"] == "context_changed"
    assert listing["history"][0]["alert_id"] == result["alert_id"]


async def test_list_never_consumes_a_firing():
    adapter = Adapter()
    keeper = AlertKeeper(adapter)
    await keeper.set_alert("q", "above", 10, "m")
    adapter.sample = sample(11)
    assert len((await keeper.list_alerts())["alerts"]) == 1
    assert len(keeper.observe(adapter.sample)) == 1


@pytest.mark.parametrize(
    "direction,start,values",
    [
        ("above", 0, [2, 4, 6]),
        ("below", 20, [18, 16, 14]),
    ],
)
async def test_stable_lead_time(direction, start, values):
    keeper = AlertKeeper(Adapter(start))
    await keeper.set_alert("q", direction, 10, "m")
    assert keeper.observe(sample(values[0], 0.5)) == []
    assert keeper.observe(sample(values[1], 1)) == []
    events = keeper.observe(sample(values[2], 1.5))
    assert len(events) == 1  # crossing still 1s away
    assert "reaching" in events[0].data["summary"]


@pytest.mark.parametrize(
    "values,times,predictable,lead",
    [
        ([2, 4, 6], [0.5, 1, 1.5], False, 1.5),  # warp
        ([2, 4, 6], [0.5, 1, 1.5], True, 0),  # disabled
        ([2, 4, 6], [0.5, 1, 1.5], True, 0.5),  # too distant
        ([1, 2, 6], [0.5, 1, 1.5], True, 1.5),  # accelerating
        ([0, 0, 0], [0.5, 1, 1.5], True, 1.5),  # zero rate
        ([-2, -4, -6], [0.5, 1, 1.5], True, 1.5),  # moving away
        ([2, 4, 6], [0.1, 0.2, 0.3], True, 1.5),  # abnormal short spacing
        ([2, 4, 6], [2, 4, 6], True, 1.5),  # abnormal long spacing
        ([2, 4, 6], [0.3, 0.6, 1.8], True, 1.5),  # inconsistent spacing
    ],
)
async def test_lead_guards_fall_back_to_actual_crossing(values, times, predictable, lead):
    adapter = Adapter()
    adapter.sample = replace(adapter.sample, predictable=predictable)
    keeper = AlertKeeper(adapter, {"lead_s": lead})
    await keeper.set_alert("q", "above", 10, "m")
    for value, timestamp in zip(values, times, strict=True):
        assert keeper.observe(sample(value, timestamp, predictable=predictable)) == []
    assert len(keeper.observe(sample(12, times[-1] + 0.5, predictable=predictable))) == 1


async def test_warp_and_invalid_reset_prediction_window():
    keeper = AlertKeeper(Adapter())
    await keeper.set_alert("q", "above", 10, "m")
    assert keeper.observe(sample(2, 0.5)) == []
    assert keeper.observe(sample(4, 1, predictable=False)) == []
    assert keeper.observe(sample(6, 1.5)) == []
    assert keeper.observe(sample(8, 2)) == []
    assert len(keeper.observe(sample(10, 2.5))) == 1


async def test_repeated_timestamp_does_not_predict_and_unknown_clock_rebaselines():
    keeper = AlertKeeper(Adapter())
    await keeper.set_alert("q", "above", 10, "m")
    for _ in range(4):
        assert keeper.observe(sample(6, 0)) == []
    assert keeper.observe(sample(6, -1)) == []
    assert len(keeper.observe(sample(10, -0.5))) == 1


async def test_limits_cancel_list_and_context_refresh():
    adapter = Adapter()
    keeper = AlertKeeper(adapter, {"max_alerts": 2})
    first = await keeper.set_alert("q", "above", 10, "m")
    second = await keeper.set_alert("q", "above", 20, "m")
    assert "error" in await keeper.set_alert("q", "above", 30, "m")
    assert len((await keeper.list_alerts())["alerts"]) == 2
    assert "error" in await keeper.cancel_alert("missing")
    assert (await keeper.cancel_alert(first["alert_id"]))["alert_ids"] == [first["alert_id"]]
    adapter.sample = sample(0, context="new vessel")
    listing = await keeper.list_alerts()
    assert listing["alerts"] == []
    assert listing["history"][-1]["alert_id"] == second["alert_id"]
    assert listing["history"][-1]["status"] == "context_changed"
    await keeper.set_alert("q", "above", 30, "m")
    assert len((await keeper.cancel_alert("all"))["alert_ids"]) == 1
    assert not keeper._active.is_set()


async def test_default_maximum_and_tool_registration():
    keeper = AlertKeeper(Adapter())
    registry = ToolRegistry()
    keeper.register(registry)
    for _ in range(8):
        assert (
            await registry.execute(
                ToolCall(
                    "x",
                    "set_alert",
                    {"quantity": "q", "direction": "above", "value": 10, "unit": "m"},
                )
            )
        )["status"] == "created"
    assert "error" in await keeper.set_alert("q", "above", 10, "m")
    assert len((await registry.execute(ToolCall("x", "list_alerts", {})))["alerts"]) == 8
    assert (
        len(
            (await registry.execute(ToolCall("x", "cancel_alert", {"alert_id": "all"})))[
                "alert_ids"
            ]
        )
        == 8
    )


async def test_poll_task_sleeps_when_idle_and_emits():
    adapter = Adapter()
    keeper = AlertKeeper(adapter)
    emitted = asyncio.Event()
    events = []

    def submit(event):
        events.append(event)
        emitted.set()

    task = asyncio.create_task(keeper.run(submit))
    try:
        await asyncio.sleep(0)
        assert adapter.reads == 0
        await keeper.set_alert("q", "above", 10, "m")
        adapter.sample = sample(10)
        await asyncio.wait_for(emitted.wait(), 1)
        assert len(events) == 1
        assert not keeper.alerts
        reads = adapter.reads
        await asyncio.sleep(0.6)
        assert adapter.reads == reads
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_simultaneous_alerts_are_not_collapsed_by_director():
    director = Director(0.5, {})
    director.set_game_state(True, False)
    for alert_id in ("a", "b"):
        director.submit(
            GameEvent(
                "alert",
                "critical",
                {
                    "summary": "A threshold",
                    "alert_id": alert_id,
                    "ttl_s": 10,
                },
                datetime.now(UTC),
            )
        )
    assert [director._take().data["alert_id"] for _ in range(2)] == ["a", "b"]


@pytest.mark.parametrize(
    "section",
    [
        "alerts = false",
        "[alerts]\nenabled = 1",
        "[alerts]\nlead_s = -1",
        "[alerts]\nlead_s = true",
        "[alerts]\nlead_s = nan",
        "[alerts]\nlead_s = inf",
        '[alerts]\nlead_s = "1"',
        "[alerts]\nmax_alerts = 0",
        "[alerts]\nmax_alerts = 9",
        "[alerts]\nmax_alerts = 2.5",
        "[alerts]\nmax_alerts = true",
    ],
)
def test_bad_config(tmp_path, section):
    path = tmp_path / "config.toml"
    path.write_text(section)
    with pytest.raises(ConfigError, match="alerts"):
        load_config(path)


def test_config_defaults_and_overrides(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("")
    assert load_config(path).alerts == {"enabled": True, "lead_s": 1.5, "max_alerts": 8}
    path.write_text("[alerts]\nenabled = false\nlead_s = 0\nmax_alerts = 2")
    assert load_config(path).alerts == {"enabled": False, "lead_s": 0, "max_alerts": 2}
