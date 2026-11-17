"""Tests for KSP hands (actions) controller and adapter integration."""

import time
from types import SimpleNamespace as Ns

import pytest
from familiar.hands import ActionKeeper
from familiar.interfaces import Actuator
from familiar_ksp.actions import SAS_MODES, KspActionController
from familiar_ksp.adapter import KspAdapter
from familiar_ksp.telemetry import Snapshot


class FakeVesselControl:
    def __init__(self):
        self.current_stage = 3
        self.abort = False
        self.sas = False
        self.sas_mode = Ns(name="stability_assist")
        self.rcs = False
        self.gear = True
        self.lights = False
        self.brakes = False
        self.throttle = 0.0
        self.action_groups = {}
        self.staged = False

    def activate_next_stage(self):
        self.staged = True
        if self.current_stage is not None:
            self.current_stage = max(0, self.current_stage - 1)

    def get_action_group(self, group: int) -> bool:
        return self.action_groups.get(group, False)

    def set_action_group(self, group: int, state: bool) -> None:
        self.action_groups[group] = bool(state)


class FakeSpaceCenter:
    def __init__(self, vessel=None):
        self.active_vessel = vessel
        self._rails_warp_factor = 0
        self.physics_warp_factor = 0
        self.warp_rate = 1.0
        self.maximum_rails_warp_factor = 7
        self.SASMode = Ns(**{mode: Ns(name=mode) for mode in SAS_MODES})
        self._warp_factor = None

    @property
    def rails_warp_factor(self) -> int:
        return self._rails_warp_factor

    @rails_warp_factor.setter
    def rails_warp_factor(self, value: int) -> None:
        self._rails_warp_factor = value
        self._warp_factor = None

    @property
    def warp_factor(self) -> int:
        if self._warp_factor is not None:
            return self._warp_factor
        return max(self._rails_warp_factor, self.physics_warp_factor)

    @warp_factor.setter
    def warp_factor(self, value: int | None) -> None:
        self._warp_factor = value

    def can_rails_warp_at(self, factor: int) -> bool:
        return factor <= self.maximum_rails_warp_factor


class FakeConnection:
    def __init__(self, vessel_name="Kerbal X", vessel_id=42):
        self.control = FakeVesselControl()
        self.vessel = Ns(name=vessel_name, _object_id=vessel_id, control=self.control)
        self.space_center = FakeSpaceCenter(self.vessel)
        self.krpc = Ns(game_scene=Ns(name="flight"), paused=False)
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def fake_connection():
    return FakeConnection()


@pytest.fixture
def controller(fake_connection):
    return KspActionController(
        {"action_timeout_s": 2.0},
        connect=lambda **kwargs: fake_connection,
    )


@pytest.mark.asyncio
async def test_isinstance_actuator():
    adapter = KspAdapter({})
    assert isinstance(adapter, Actuator)
    await adapter.aclose()


def test_action_specs_and_risks(controller):
    actions = controller.actions()
    assert len(actions) == 11

    # Risky actions
    assert actions["stage"].risky is True
    assert actions["stage"].risky_when is None
    assert actions["abort"].risky is True
    assert actions["abort"].risky_when is None
    assert actions["action_group"].risky is True
    assert actions["action_group"].risky_when is None
    assert actions["throttle"].risky is True
    assert actions["throttle"].risky_when is None

    # Time warp: harmless factor 0, risky factor > 0
    assert actions["time_warp"].risky is False
    assert actions["time_warp"].risky_when({"factor": 0}) is False
    assert actions["time_warp"].risky_when({"factor": 1}) is True
    assert actions["time_warp"].risky_when({"factor": 5}) is True

    # Harmless actions
    for name in ("sas", "sas_mode", "rcs", "gear", "lights", "brakes"):
        assert actions[name].risky is False
        assert actions[name].risky_when is None


@pytest.mark.asyncio
async def test_guards(controller, fake_connection):
    try:
        # Scene not flight
        fake_connection.krpc.game_scene = Ns(name="editor_vab")
        res = await controller.perform("lights", {"state": "on"})
        assert res["status"] == "unavailable"
        assert "flight" in res["detail"]

        # Game paused
        fake_connection.krpc.game_scene = Ns(name="flight")
        fake_connection.krpc.paused = True
        res = await controller.perform("lights", {"state": "on"})
        assert res["status"] == "unavailable"
        assert "paused" in res["detail"]

        # No active vessel
        fake_connection.krpc.paused = False
        fake_connection.space_center.active_vessel = None
        res = await controller.perform("lights", {"state": "on"})
        assert res["status"] == "unavailable"
        assert "no active vessel" in res["detail"]
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_stage_action(controller, fake_connection):
    try:
        res = await controller.perform("stage", {})
        assert res["status"] == "done"
        assert "staged to stage 2" in res["detail"]
        assert fake_connection.control.current_stage == 2
        assert fake_connection.control.staged is True

        # Unknown when stage cannot be read
        fake_connection.control.current_stage = None
        res = await controller.perform("stage", {})
        assert res["status"] == "unknown"
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_abort_action(controller, fake_connection):
    try:
        # Not yet aborted -> done
        fake_connection.control.abort = False
        res = await controller.perform("abort", {})
        assert res["status"] == "done"
        assert fake_connection.control.abort is True

        # Already aborted -> already
        res = await controller.perform("abort", {})
        assert res["status"] == "already"

        # Unknown when setter has no effect
        class BrokenControl(FakeVesselControl):
            @property
            def abort(self):
                return False

            @abort.setter
            def abort(self, val):
                pass

        fake_connection.vessel.control = BrokenControl()
        res = await controller.perform("abort", {})
        assert res["status"] == "unknown"
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_sas_actions(controller, fake_connection):
    try:
        fake_connection.control.sas = False

        # Turn on -> done
        res = await controller.perform("sas", {"state": "on"})
        assert res["status"] == "done"
        assert fake_connection.control.sas is True

        # Turn on again -> already
        res = await controller.perform("sas", {"state": "on"})
        assert res["status"] == "already"

        # Turn off -> done
        res = await controller.perform("sas", {"state": "off"})
        assert res["status"] == "done"
        assert fake_connection.control.sas is False

        # Read back mismatch -> unknown
        class BrokenSAS(FakeVesselControl):
            def __init__(self):
                super().__init__()
                self._sas = False

            @property
            def sas(self):
                return False

            @sas.setter
            def sas(self, v):
                pass

        fake_connection.vessel.control = BrokenSAS()
        res = await controller.perform("sas", {"state": "on"})
        assert res["status"] == "unknown"
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_sas_mode_actions(controller, fake_connection):
    try:
        fake_connection.control.sas = False
        fake_connection.control.sas_mode = Ns(name="stability_assist")

        # Sets mode and enables SAS if off
        res = await controller.perform("sas_mode", {"mode": "prograde"})
        assert res["status"] == "done"
        assert fake_connection.control.sas is True
        assert fake_connection.control.sas_mode.name == "prograde"

        # Already in that mode with SAS on -> already
        res = await controller.perform("sas_mode", {"mode": "prograde"})
        assert res["status"] == "already"

        # Read back mismatch -> unknown
        fake_connection.control.sas_mode = Ns(name="normal")
        res = await controller.perform("sas_mode", {"mode": "retrograde"})

        # Target was set, now make getter return something else
        class StubModeControl(FakeVesselControl):
            def __init__(self):
                super().__init__()
                self.sas = True

            @property
            def sas_mode(self):
                return Ns(name="stability_assist")

            @sas_mode.setter
            def sas_mode(self, v):
                pass

        fake_connection.vessel.control = StubModeControl()
        res = await controller.perform("sas_mode", {"mode": "retrograde"})
        assert res["status"] == "unknown"

        # Exception when setting mode -> unavailable
        class ErrorModeControl(FakeVesselControl):
            def __init__(self):
                super().__init__()
                self.armed = True

            @property
            def sas_mode(self):
                return Ns(name="stability_assist")

            @sas_mode.setter
            def sas_mode(self, v):
                if getattr(self, "armed", False):
                    raise RuntimeError("no target selected")

        fake_connection.vessel.control = ErrorModeControl()
        res = await controller.perform("sas_mode", {"mode": "target"})
        assert res["status"] == "unavailable"
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_rcs_actions(controller, fake_connection):
    try:
        fake_connection.control.rcs = False
        res = await controller.perform("rcs", {"state": "on"})
        assert res["status"] == "done"
        assert fake_connection.control.rcs is True

        res = await controller.perform("rcs", {"state": "on"})
        assert res["status"] == "already"

        res = await controller.perform("rcs", {"state": "off"})
        assert res["status"] == "done"
        assert fake_connection.control.rcs is False

        # Read back mismatch -> unknown
        class BrokenRcs(FakeVesselControl):
            @property
            def rcs(self):
                return False

            @rcs.setter
            def rcs(self, v):
                pass

        fake_connection.vessel.control = BrokenRcs()
        res = await controller.perform("rcs", {"state": "on"})
        assert res["status"] == "unknown"
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_gear_actions(controller, fake_connection):
    try:
        # Gear starts down (True)
        fake_connection.control.gear = True
        res = await controller.perform("gear", {"state": "down"})
        assert res["status"] == "already"

        res = await controller.perform("gear", {"state": "up"})
        assert res["status"] == "done"
        assert fake_connection.control.gear is False

        res = await controller.perform("gear", {"state": "up"})
        assert res["status"] == "already"

        res = await controller.perform("gear", {"state": "down"})
        assert res["status"] == "done"
        assert fake_connection.control.gear is True

        # Read back mismatch -> unknown
        class BrokenGear(FakeVesselControl):
            @property
            def gear(self):
                return False

            @gear.setter
            def gear(self, v):
                pass

        fake_connection.vessel.control = BrokenGear()
        res = await controller.perform("gear", {"state": "down"})
        assert res["status"] == "unknown"
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_lights_and_brakes_actions(controller, fake_connection):
    try:
        # Lights
        fake_connection.control.lights = False
        res = await controller.perform("lights", {"state": "on"})
        assert res["status"] == "done"
        assert fake_connection.control.lights is True

        res = await controller.perform("lights", {"state": "on"})
        assert res["status"] == "already"

        # Brakes
        fake_connection.control.brakes = False
        res = await controller.perform("brakes", {"state": "on"})
        assert res["status"] == "done"
        assert fake_connection.control.brakes is True

        res = await controller.perform("brakes", {"state": "on"})
        assert res["status"] == "already"
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_action_group_action(controller, fake_connection):
    try:
        # Toggle 1 from False to True
        res = await controller.perform("action_group", {"group": 1})
        assert res["status"] == "done"
        assert "toggled on" in res["detail"]
        assert fake_connection.control.get_action_group(1) is True

        # Toggle 1 from True to False
        res = await controller.perform("action_group", {"group": 1})
        assert res["status"] == "done"
        assert "toggled off" in res["detail"]
        assert fake_connection.control.get_action_group(1) is False

        # Read back mismatch -> unknown
        class BrokenGroup(FakeVesselControl):
            def set_action_group(self, group, state):
                pass

        fake_connection.vessel.control = BrokenGroup()
        res = await controller.perform("action_group", {"group": 2})
        assert res["status"] == "unknown"
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_throttle_action(controller, fake_connection):
    try:
        fake_connection.control.throttle = 0.0
        res = await controller.perform("throttle", {"value": 0.5})
        assert res["status"] == "done"
        assert fake_connection.control.throttle == 0.5

        # Already at 0.5
        res = await controller.perform("throttle", {"value": 0.5})
        assert res["status"] == "already"

        # Read back mismatch -> unknown
        class BrokenThrottle(FakeVesselControl):
            @property
            def throttle(self):
                return 0.0

            @throttle.setter
            def throttle(self, v):
                pass

        fake_connection.vessel.control = BrokenThrottle()
        res = await controller.perform("throttle", {"value": 0.9})
        assert res["status"] == "unknown"
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_time_warp_action(controller, fake_connection):
    sc = fake_connection.space_center
    try:
        # Factor 0 already stopped
        sc.warp_factor = 0
        sc.warp_rate = 1.0
        res = await controller.perform("time_warp", {"factor": 0})
        assert res["status"] == "already"

        # Factor 0 when active
        sc.warp_factor = 3
        sc.rails_warp_factor = 3
        res = await controller.perform("time_warp", {"factor": 0})
        assert res["status"] == "done"
        assert sc.rails_warp_factor == 0

        # Factor > 0 already at that factor
        sc.rails_warp_factor = 4
        res = await controller.perform("time_warp", {"factor": 4})
        assert res["status"] == "already"

        # Factor > 0 set successfully
        sc.rails_warp_factor = 0
        sc.maximum_rails_warp_factor = 7
        res = await controller.perform("time_warp", {"factor": 5})
        assert res["status"] == "done"
        assert "factor 5" in res["detail"]
        assert sc.rails_warp_factor == 5

        # Limited by maximum_rails_warp_factor
        sc.rails_warp_factor = 0
        sc.maximum_rails_warp_factor = 2
        res = await controller.perform("time_warp", {"factor": 5})
        assert res["status"] == "unavailable"

        # Limited by can_rails_warp_at
        sc.maximum_rails_warp_factor = 7
        sc.can_rails_warp_at = lambda f: False
        res = await controller.perform("time_warp", {"factor": 3})
        assert res["status"] == "unavailable"

        # Clamped by game (reached < factor)
        sc.can_rails_warp_at = lambda f: True

        class ClampingSpaceCenter(FakeSpaceCenter):
            @property
            def rails_warp_factor(self):
                return getattr(self, "_f", 0)

            @rails_warp_factor.setter
            def rails_warp_factor(self, v):
                self._f = min(v, 2)

        fake_connection.space_center = ClampingSpaceCenter(fake_connection.vessel)
        res = await controller.perform("time_warp", {"factor": 5})
        assert res["status"] == "unavailable"
        assert "limited to 2" in res["detail"]
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_argument_validation(controller):
    try:
        # Unknown action
        res = await controller.perform("warp_drive", {})
        assert res["status"] == "error"

        # Invalid state
        res = await controller.perform("sas", {"state": "invalid"})
        assert res["status"] == "error"

        # Invalid mode
        res = await controller.perform("sas_mode", {"mode": "sideways"})
        assert res["status"] == "error"

        # Invalid gear
        res = await controller.perform("gear", {"state": "halfway"})
        assert res["status"] == "error"

        # Invalid action group
        res = await controller.perform("action_group", {"group": 0})
        assert res["status"] == "error"
        res = await controller.perform("action_group", {"group": 11})
        assert res["status"] == "error"
        res = await controller.perform("action_group", {"group": "1"})
        assert res["status"] == "error"

        # Invalid throttle
        res = await controller.perform("throttle", {"value": -0.1})
        assert res["status"] == "error"
        res = await controller.perform("throttle", {"value": 1.1})
        assert res["status"] == "error"
        res = await controller.perform("throttle", {"value": "half"})
        assert res["status"] == "error"

        # Invalid time warp
        res = await controller.perform("time_warp", {"factor": -1})
        assert res["status"] == "error"
        res = await controller.perform("time_warp", {"factor": 8})
        assert res["status"] == "error"
        res = await controller.perform("time_warp", {"factor": True})
        assert res["status"] == "error"
    finally:
        await controller.aclose()


@pytest.mark.asyncio
async def test_timeout_and_no_retry(fake_connection):
    calls = []

    def slow_stage():
        calls.append(1)
        time.sleep(0.2)

    fake_connection.control.activate_next_stage = slow_stage
    ctrl = KspActionController(
        {"action_timeout_s": 0.02},
        connect=lambda **kwargs: fake_connection,
    )
    try:
        res = await ctrl.perform("stage", {})
        assert res["status"] == "unknown"
        assert "timed out" in res["detail"]
        assert len(calls) == 1
        assert ctrl.connection is None
    finally:
        await ctrl.aclose()


@pytest.mark.asyncio
async def test_reconnect_after_drop(fake_connection):
    connect_calls = []

    def connect(**kwargs):
        connect_calls.append(kwargs)
        return fake_connection

    ctrl = KspActionController({"action_timeout_s": 1.0}, connect=connect)
    try:
        res = await ctrl.perform("lights", {"state": "on"})
        assert res["status"] == "done"
        assert len(connect_calls) == 1
        assert connect_calls[0]["name"] == "Familiar actions"

        # Simulate lost connection / drop
        fake_connection.krpc = None  # access raises AttributeError / Exception
        res = await ctrl.perform("lights", {"state": "off"})
        assert res["status"] in ("unknown", "error")
        assert ctrl.connection is None

        # Fix connection and next perform reconnects freshly
        fake_connection.krpc = Ns(game_scene=Ns(name="flight"), paused=False)
        fake_connection.control.lights = False
        res = await ctrl.perform("lights", {"state": "on"})
        assert res["status"] == "done"
        assert len(connect_calls) == 2
    finally:
        await ctrl.aclose()


@pytest.mark.asyncio
async def test_vessel_change_context_with_action_keeper(fake_connection):
    adapter = KspAdapter({})
    adapter.actions_controller.connect = lambda **kwargs: fake_connection
    try:
        # Initial vessel Kerbal 1
        adapter.latest = Snapshot(
            connected=True,
            scene="flight",
            vessel_name="Kerbal 1",
            vessel_id="101",
        )
        assert adapter.action_context() == "Kerbal 1:101"

        keeper = ActionKeeper(adapter, {}, "en")
        keeper.begin_turn("stage the rocket")

        # Request risky action -> returns pending
        pending = await keeper.request("stage", adapter.actions()["stage"])
        assert pending["status"] == "needs_confirmation"
        confirmation_id = pending["confirmation_id"]

        # Vessel switches before confirmation
        adapter.latest = Snapshot(
            connected=True,
            scene="flight",
            vessel_name="Kerbal 2",
            vessel_id="102",
        )
        assert adapter.action_context() == "Kerbal 2:102"

        # Confirmation turn with "yes"
        keeper.begin_turn("yes, confirm")
        res = await keeper.confirm_action(confirmation_id)
        assert res["status"] == "error"
        assert "Action context changed" in res["detail"]
    finally:
        await adapter.aclose()
