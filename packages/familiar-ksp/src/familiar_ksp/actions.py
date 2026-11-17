"""KSP action controller implementing hands via a dedicated kRPC connection."""

import asyncio
import math
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from typing import Any

import krpc
from familiar.interfaces import ActionSpec

from .telemetry import enum_name, positive

SAS_MODES = (
    "stability_assist",
    "prograde",
    "retrograde",
    "normal",
    "anti_normal",
    "radial",
    "anti_radial",
    "target",
    "anti_target",
    "maneuver",
)

ACTION_SPECS = {
    "stage": ActionSpec(
        description="Stage the active vessel to activate the next stage.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        risky=True,
    ),
    "abort": ActionSpec(
        description="Trigger the abort action group.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        risky=True,
    ),
    "sas": ActionSpec(
        description="Turn Stability Augmentation System (SAS) on or off.",
        parameters={
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "enum": ["on", "off"],
                    "description": "Target SAS state.",
                }
            },
            "required": ["state"],
            "additionalProperties": False,
        },
        risky=False,
    ),
    "sas_mode": ActionSpec(
        description="Set SAS hold mode (turns SAS on if needed).",
        parameters={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": list(SAS_MODES),
                    "description": "SAS mode to set.",
                }
            },
            "required": ["mode"],
            "additionalProperties": False,
        },
        risky=False,
    ),
    "rcs": ActionSpec(
        description="Turn Reaction Control System (RCS) on or off.",
        parameters={
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "enum": ["on", "off"],
                    "description": "Target RCS state.",
                }
            },
            "required": ["state"],
            "additionalProperties": False,
        },
        risky=False,
    ),
    "gear": ActionSpec(
        description="Set landing gear up (retracted) or down (deployed).",
        parameters={
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "enum": ["up", "down"],
                    "description": "Target landing gear state.",
                }
            },
            "required": ["state"],
            "additionalProperties": False,
        },
        risky=False,
    ),
    "lights": ActionSpec(
        description="Turn vessel lights on or off.",
        parameters={
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "enum": ["on", "off"],
                    "description": "Target lights state.",
                }
            },
            "required": ["state"],
            "additionalProperties": False,
        },
        risky=False,
    ),
    "brakes": ActionSpec(
        description="Turn vessel brakes on or off.",
        parameters={
            "type": "object",
            "properties": {
                "state": {
                    "type": "string",
                    "enum": ["on", "off"],
                    "description": "Target brakes state.",
                }
            },
            "required": ["state"],
            "additionalProperties": False,
        },
        risky=False,
    ),
    "action_group": ActionSpec(
        description="Toggle a vessel action group (1 to 10).",
        parameters={
            "type": "object",
            "properties": {
                "group": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "description": "Action group number from 1 to 10.",
                }
            },
            "required": ["group"],
            "additionalProperties": False,
        },
        risky=True,
    ),
    "throttle": ActionSpec(
        description="Set vessel throttle from 0.0 to 1.0.",
        parameters={
            "type": "object",
            "properties": {
                "value": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Throttle level from 0.0 (idle) to 1.0 (full).",
                }
            },
            "required": ["value"],
            "additionalProperties": False,
        },
        risky=True,
    ),
    "time_warp": ActionSpec(
        description="Set time warp factor (0 to stop warp, 1 to 7 for on-rails warp).",
        parameters={
            "type": "object",
            "properties": {
                "factor": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 7,
                    "description": "Target warp factor from 0 to 7.",
                }
            },
            "required": ["factor"],
            "additionalProperties": False,
        },
        risky=False,
        risky_when=lambda args: args.get("factor", 0) > 0,
    ),
}


def _val_no_args(args: dict) -> str | None:
    return None


def _val_state(args: dict) -> str | None:
    state = args.get("state")
    if state not in ("on", "off"):
        return f"'state' must be 'on' or 'off', got {state!r}"
    return None


def _val_gear(args: dict) -> str | None:
    state = args.get("state")
    if state not in ("up", "down"):
        return f"'state' must be 'up' or 'down', got {state!r}"
    return None


def _val_sas_mode(args: dict) -> str | None:
    mode = args.get("mode")
    if mode not in SAS_MODES:
        return f"'mode' must be one of {SAS_MODES}, got {mode!r}"
    return None


def _val_action_group(args: dict) -> str | None:
    group = args.get("group")
    if isinstance(group, bool) or not isinstance(group, int) or not (1 <= group <= 10):
        return f"'group' must be an integer between 1 and 10, got {group!r}"
    return None


def _val_throttle(args: dict) -> str | None:
    val = args.get("value")
    if (
        isinstance(val, bool)
        or not isinstance(val, (int, float))
        or not math.isfinite(val)
        or not (0.0 <= val <= 1.0)
    ):
        return f"'value' must be a finite number between 0.0 and 1.0, got {val!r}"
    return None


def _val_time_warp(args: dict) -> str | None:
    factor = args.get("factor")
    if isinstance(factor, bool) or not isinstance(factor, int) or not (0 <= factor <= 7):
        return f"'factor' must be an integer between 0 and 7, got {factor!r}"
    return None


VALIDATORS: dict[str, Callable[[dict], str | None]] = {
    "stage": _val_no_args,
    "abort": _val_no_args,
    "sas": _val_state,
    "sas_mode": _val_sas_mode,
    "rcs": _val_state,
    "gear": _val_gear,
    "lights": _val_state,
    "brakes": _val_state,
    "action_group": _val_action_group,
    "throttle": _val_throttle,
    "time_warp": _val_time_warp,
}


def _act_stage(conn: Any, sc: Any, vessel: Any, args: dict) -> dict:
    vessel.control.activate_next_stage()
    new_stage = getattr(vessel.control, "current_stage", None)
    if new_stage is None:
        return {"status": "unknown", "detail": "staged, but new stage is unknown"}
    return {"status": "done", "detail": f"staged to stage {new_stage}"}


def _act_abort(conn: Any, sc: Any, vessel: Any, args: dict) -> dict:
    if getattr(vessel.control, "abort", False):
        return {"status": "already", "detail": "abort already triggered"}
    vessel.control.abort = True
    if not getattr(vessel.control, "abort", False):
        return {"status": "unknown", "detail": "abort trigger not observed"}
    return {"status": "done", "detail": "abort triggered"}


# Boolean control flags: (kRPC attribute, label, word for True, word for False).
FLAGS = {
    "sas": ("sas", "SAS", "on", "off"),
    "rcs": ("rcs", "RCS", "on", "off"),
    "gear": ("gear", "gear", "down", "up"),
    "lights": ("lights", "lights", "on", "off"),
    "brakes": ("brakes", "brakes", "on", "off"),
}


def _set_flag(name: str, vessel: Any, args: dict) -> dict:
    attribute, label, true_word, false_word = FLAGS[name]
    target = args["state"] == true_word
    if bool(getattr(vessel.control, attribute, False)) == target:
        return {"status": "already", "detail": f"{label} already {args['state']}"}
    setattr(vessel.control, attribute, target)
    observed = bool(getattr(vessel.control, attribute, False))
    if observed != target:
        word = true_word if observed else false_word
        return {"status": "unknown", "detail": f"{label} state unknown (observed {word})"}
    return {"status": "done", "detail": f"{label} {args['state']}"}


def _flag_handler(name: str) -> Callable[[Any, Any, Any, dict], dict]:
    return lambda conn, sc, vessel, args: _set_flag(name, vessel, args)


def _act_sas_mode(conn: Any, sc: Any, vessel: Any, args: dict) -> dict:
    mode_str = args["mode"]
    sas_on = bool(getattr(vessel.control, "sas", False))
    current_mode = enum_name(getattr(vessel.control, "sas_mode", None))
    if sas_on and current_mode == mode_str:
        return {"status": "already", "detail": f"SAS mode already {mode_str}"}
    if not sas_on:
        vessel.control.sas = True
    target_mode = getattr(sc.SASMode, mode_str, mode_str) if hasattr(sc, "SASMode") else mode_str
    try:
        vessel.control.sas_mode = target_mode
    except Exception as exc:
        return {"status": "unavailable", "detail": f"cannot set SAS mode {mode_str}: {exc}"}
    observed_sas = bool(getattr(vessel.control, "sas", False))
    observed_mode = enum_name(getattr(vessel.control, "sas_mode", None))
    if not observed_sas or observed_mode != mode_str:
        sas_text = "on" if observed_sas else "off"
        return {
            "status": "unknown",
            "detail": f"SAS mode unknown (observed {observed_mode}, SAS {sas_text})",
        }
    return {"status": "done", "detail": f"SAS mode set to {mode_str}"}


def _act_action_group(conn: Any, sc: Any, vessel: Any, args: dict) -> dict:
    group = int(args["group"])
    current = bool(vessel.control.get_action_group(group))
    target = not current
    vessel.control.set_action_group(group, target)
    observed = bool(vessel.control.get_action_group(group))
    if observed != target:
        return {"status": "unknown", "detail": f"action group {group} state unknown"}
    state_str = "on" if observed else "off"
    return {"status": "done", "detail": f"action group {group} toggled {state_str}"}


def _act_throttle(conn: Any, sc: Any, vessel: Any, args: dict) -> dict:
    value = float(args["value"])
    current = float(getattr(vessel.control, "throttle", 0.0))
    if math.isclose(current, value, abs_tol=0.005):
        return {"status": "already", "detail": f"throttle already {value:.2f}"}
    vessel.control.throttle = value
    observed = float(getattr(vessel.control, "throttle", 0.0))
    if not math.isclose(observed, value, abs_tol=0.005):
        return {"status": "unknown", "detail": f"throttle state unknown (observed {observed:.2f})"}
    return {"status": "done", "detail": f"throttle set to {observed:.2f}"}


def _act_time_warp(conn: Any, sc: Any, vessel: Any, args: dict) -> dict:
    factor = int(args["factor"])
    if factor == 0:
        current_factor = getattr(sc, "warp_factor", getattr(sc, "rails_warp_factor", 0))
        current_rate = getattr(sc, "warp_rate", 1.0)
        if current_factor == 0 and current_rate == 1.0:
            return {"status": "already", "detail": "warp already stopped (factor 0)"}
        if hasattr(sc, "rails_warp_factor"):
            sc.rails_warp_factor = 0
        if hasattr(sc, "physics_warp_factor"):
            sc.physics_warp_factor = 0
        observed = getattr(sc, "warp_factor", getattr(sc, "rails_warp_factor", 0))
        if observed != 0:
            return {
                "status": "unknown",
                "detail": f"time warp factor unknown (observed {observed})",
            }
        return {"status": "done", "detail": "time warp stopped (factor 0)"}

    current_factor = getattr(sc, "rails_warp_factor", getattr(sc, "warp_factor", 0))
    if current_factor == factor:
        return {"status": "already", "detail": f"time warp already at factor {factor}"}

    if hasattr(sc, "can_rails_warp_at") and not sc.can_rails_warp_at(factor):
        max_f = getattr(sc, "maximum_rails_warp_factor", None)
        detail = (
            f"rails warp factor {factor} unavailable (maximum is {max_f})"
            if max_f is not None
            else f"rails warp factor {factor} unavailable in current situation"
        )
        return {"status": "unavailable", "detail": detail}

    max_factor = getattr(sc, "maximum_rails_warp_factor", None)
    if max_factor is not None and max_factor < factor:
        return {
            "status": "unavailable",
            "detail": f"rails warp factor {factor} unavailable (maximum is {max_factor})",
        }

    try:
        sc.rails_warp_factor = factor
    except Exception as exc:
        return {"status": "unavailable", "detail": f"time warp factor {factor} unavailable: {exc}"}

    reached = getattr(sc, "rails_warp_factor", getattr(sc, "warp_factor", None))
    if reached is None:
        return {"status": "unknown", "detail": "time warp factor unknown"}
    if reached < factor:
        return {
            "status": "unavailable",
            "detail": f"time warp factor {factor} limited to {reached} by game",
        }
    if reached == factor:
        return {"status": "done", "detail": f"time warp set to factor {reached}"}
    return {"status": "unknown", "detail": f"time warp factor unknown (observed {reached})"}


HANDLERS: dict[str, Callable[[Any, Any, Any, dict], dict]] = {
    "stage": _act_stage,
    "abort": _act_abort,
    "sas": _flag_handler("sas"),
    "sas_mode": _act_sas_mode,
    "rcs": _flag_handler("rcs"),
    "gear": _flag_handler("gear"),
    "lights": _flag_handler("lights"),
    "brakes": _flag_handler("brakes"),
    "action_group": _act_action_group,
    "throttle": _act_throttle,
    "time_warp": _act_time_warp,
}


class KspActionController:
    """Dedicated action connection to kRPC executing game actions."""

    def __init__(
        self,
        settings: dict | None = None,
        *,
        connect: Any = None,
        context_provider: Callable[[], str | None] | None = None,
    ) -> None:
        self.settings = settings or {}
        raw_timeout = self.settings.get("action_timeout_s")
        if raw_timeout is None:
            raw_timeout = self.settings.get("timeout_s", 3.0)
        self.timeout = positive(raw_timeout, "action_timeout_s")
        self.connect = connect or krpc.connect
        self.context_provider = context_provider
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="familiar-ksp-actions")
        self.connection: Any = None
        self.closed = False
        self._vessel_context: str | None = None

    def actions(self) -> dict[str, ActionSpec]:
        return ACTION_SPECS

    def action_context(self) -> str | None:
        if self.context_provider is not None:
            ctx = self.context_provider()
            if ctx is not None:
                return ctx
        return self._vessel_context

    def _close(self) -> None:
        connection, self.connection = self.connection, None
        if connection is None:
            return
        with suppress(Exception):
            connection.close()

    def _run_action(self, name: str, arguments: dict) -> dict:
        try:
            if self.connection is None:
                self.connection = self.connect(
                    name="Familiar actions",
                    address=self.settings.get("address", "127.0.0.1"),
                    rpc_port=int(self.settings.get("rpc_port", 50000)),
                    stream_port=None,
                    timeout=self.timeout,
                )
            krpc_service = getattr(self.connection, "krpc", None)
            if krpc_service is None:
                self._close()
                return {
                    "status": "unknown",
                    "detail": "connection lost; outcome unknown, not retried",
                }
            scene = enum_name(getattr(krpc_service, "game_scene", None))
            if scene != "flight":
                return {"status": "unavailable", "detail": "not in flight"}
            if getattr(krpc_service, "paused", False):
                return {"status": "unavailable", "detail": "game is paused"}
            sc = self.connection.space_center
            vessel = getattr(sc, "active_vessel", None)
            if vessel is None:
                return {"status": "unavailable", "detail": "no active vessel"}

            vessel_id = str(getattr(vessel, "_object_id", vessel.name))
            self._vessel_context = f"{vessel.name}:{vessel_id}"

            handler = HANDLERS[name]
            return handler(self.connection, sc, vessel, arguments)
        except (OSError, EOFError, TimeoutError) as exc:
            self._close()
            return {
                "status": "unknown",
                "detail": f"connection lost ({exc}); outcome unknown, not retried",
            }
        except Exception as exc:
            err_msg = str(exc).lower()
            if any(term in err_msg for term in ("closed", "timeout", "disconnected")):
                self._close()
                return {
                    "status": "unknown",
                    "detail": f"connection lost ({exc}); outcome unknown, not retried",
                }
            return {"status": "error", "detail": str(exc)}

    async def perform(self, name: str, arguments: dict) -> dict:
        if self.closed:
            return {"status": "unavailable", "detail": "action controller closed"}
        if name not in HANDLERS:
            return {"status": "error", "detail": f"Unknown action: {name}"}
        val_err = VALIDATORS[name](arguments)
        if val_err is not None:
            return {"status": "error", "detail": f"Invalid arguments for {name}: {val_err}"}

        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(self.worker, self._run_action, name, arguments),
                timeout=self.timeout,
            )
        except TimeoutError:
            self._close()
            return {"status": "unknown", "detail": "action timed out; outcome unknown, not retried"}

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await asyncio.get_running_loop().run_in_executor(self.worker, self._close)
        finally:
            self.worker.shutdown(wait=False)
