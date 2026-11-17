"""Session-only, one-shot telemetry alerts owned by core."""

import asyncio
import logging
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from statistics import median
from uuid import uuid4

from familiar.interfaces import GameEvent, ToolSpec, Watchable, WatchSample
from familiar.tools import ToolRegistry

_LOG = logging.getLogger(__name__)

# Unit -> (quantity dimension / SI unit, multiplier).
UNITS = {
    "m": ("m", 1),
    "km": ("m", 1000),
    "ft": ("m", 0.3048),
    "mi": ("m", 1609.344),
    "nmi": ("m", 1852),
    "m/s": ("m/s", 1),
    "km/h": ("m/s", 1 / 3.6),
    "mph": ("m/s", 0.44704),
    "kn": ("m/s", 1852 / 3600),
    "s": ("s", 1),
    "min": ("s", 60),
    "h": ("s", 3600),
    "%": ("%", 1),
    "count": ("count", 1),
}


def _number(value: object) -> bool:
    return type(value) in {int, float} and math.isfinite(value)


@dataclass
class _Alert:
    id: str
    quantity: str
    direction: str
    threshold: float
    unit: str
    requested_value: float
    requested_unit: str
    description: str
    context: str | None
    label: str = ""
    announcement: str = ""  # spoken verbatim on firing, skipping a slow LLM round trip
    points: deque = field(default_factory=lambda: deque(maxlen=4))
    armed: bool = False
    predictable: bool = False

    @property
    def sign(self) -> int:
        return {"above": 1, "below": -1}[self.direction]

    @property
    def epsilon(self) -> float:
        return max(1e-9, abs(self.threshold) * 1e-9)

    def details(self) -> dict:
        return {
            "alert_id": self.id,
            "quantity": self.quantity,
            "direction": self.direction,
            "value": self.requested_value,
            "unit": self.requested_unit,
            "si_value": self.threshold,
            "si_unit": self.unit,
            "label": self.label,
            "context": self.context,
        }


class AlertKeeper:
    def __init__(self, adapter: Watchable, settings: dict | None = None) -> None:
        self.adapter = adapter
        settings = settings or {}
        self.lead_s = settings.get("lead_s", 1.0)
        self.max_alerts = min(8, settings.get("max_alerts", 8))
        self.alerts: dict[str, _Alert] = {}
        self.history: deque[dict] = deque(maxlen=32)
        self._active = asyncio.Event()

    def register(self, registry: ToolRegistry) -> None:
        quantities = self.adapter.watchables()
        properties = {
            "quantity": {"type": "string", "enum": list(quantities)},
            "direction": {"type": "string", "enum": ["above", "below"]},
            "value": {"type": "number"},
            "unit": {"type": "string", "enum": list(UNITS)},
            "label": {"type": "string"},
            "announcement": {
                "type": "string",
                "description": "Very short line said verbatim the moment it fires, in your "
                "own voice and the conversation's language, e.g. 'Ten thousand metres!'. "
                "No current values: it is spoken exactly as written.",
            },
        }
        descriptions = "; ".join(
            f"{key} ({spec.unit}): {spec.description}" for key, spec in quantities.items()
        )
        registry.register(
            ToolSpec(
                "set_alert",
                "Tell the player once a telemetry value reaches a threshold. "
                "Use below for remaining distance or fuel falling to a value. " + descriptions,
                {
                    "type": "object",
                    "properties": properties,
                    "required": ["quantity", "direction", "value", "unit", "announcement"],
                    "additionalProperties": False,
                },
            ),
            self.set_alert,
        )
        registry.register(
            ToolSpec(
                "list_alerts",
                "List active alerts and recent removals.",
                {"type": "object", "properties": {}, "additionalProperties": False},
            ),
            self.list_alerts,
        )
        registry.register(
            ToolSpec(
                "cancel_alert",
                "Cancel an alert by ID, or all alerts with 'all'.",
                {
                    "type": "object",
                    "properties": {"alert_id": {"type": "string"}},
                    "required": ["alert_id"],
                    "additionalProperties": False,
                },
            ),
            self.cancel_alert,
        )

    async def set_alert(
        self,
        quantity: str,
        direction: str,
        value: float,
        unit: str,
        label: str = "",
        announcement: str = "",
    ) -> dict:
        spec = self.adapter.watchables().get(quantity)
        if spec is None:
            return {"error": f"Unknown quantity: {quantity}"}
        if direction not in {"above", "below"}:
            return {"error": "Direction must be above or below"}
        conversion = UNITS.get(unit)
        if conversion is None or conversion[0] != spec.unit:
            return {"error": f"Unit {unit!r} is incompatible with {quantity} ({spec.unit})"}
        if not _number(value) or not math.isfinite(value * conversion[1]):
            return {"error": "Threshold must be finite"}
        sample = self.adapter.watch_sample()
        current = sample.values.get(quantity)
        if not sample.valid or not _number(current):
            return {"error": f"Quantity {quantity} is currently unavailable"}
        threshold = value * conversion[1]
        alert = _Alert(
            uuid4().hex[:12],
            quantity,
            direction,
            threshold,
            spec.unit,
            value,
            unit,
            spec.description,
            sample.context,
            label,
            announcement.strip(),
        )
        details = {**alert.details(), "current_value": current, "current_unit": spec.unit}
        if alert.sign * (current - threshold) >= 0:
            return {**details, "status": "already_satisfied", "alert_id": None}
        if len(self.alerts) >= self.max_alerts:
            return {"error": f"At most {self.max_alerts} alerts can be active"}
        self._baseline(alert, sample, current)
        self.alerts[alert.id] = alert
        self._active.set()
        return {**details, "status": "created"}

    async def list_alerts(self) -> dict:
        sample = self.adapter.watch_sample()
        for alert in list(self.alerts.values()):
            if sample.valid and sample.context != alert.context:
                self._remove(alert, "context_changed")
        return {
            "alerts": [alert.details() for alert in self.alerts.values()],
            "history": list(self.history),
        }

    async def cancel_alert(self, alert_id: str) -> dict:
        ids = list(self.alerts) if alert_id == "all" else [alert_id]
        if alert_id != "all" and alert_id not in self.alerts:
            return {"error": f"Unknown alert: {alert_id}"}
        for item in ids:
            self._remove(self.alerts[item], "cancelled")
        return {"status": "cancelled", "alert_ids": ids}

    def _remove(self, alert: _Alert, reason: str) -> None:
        self.alerts.pop(alert.id, None)
        self.history.append({**alert.details(), "status": reason})
        _LOG.info("Alert %s: %s", alert.id, reason)
        if not self.alerts:
            self._active.clear()

    @staticmethod
    def _baseline(alert: _Alert, sample: WatchSample, value: float) -> None:
        alert.points.clear()
        alert.points.append((sample.timestamp, value))
        alert.predictable = sample.predictable
        alert.armed = alert.sign * (value - alert.threshold) < -alert.epsilon

    def _early(self, alert: _Alert, sample: WatchSample, value: float) -> bool:
        if (
            not sample.predictable
            or not alert.predictable
            or self.lead_s <= 0
            or len(alert.points) < 4
        ):
            return False
        points = list(alert.points)
        spacings = [b[0] - a[0] for a, b in zip(points, points[1:], strict=False)]
        if not all(0.25 <= dt <= 1.25 for dt in spacings):
            return False
        if max(spacings) > 2.5 * min(spacings):
            return False
        slopes = [
            (b[1] - a[1]) / dt for a, b, dt in zip(points, points[1:], spacings, strict=False)
        ]
        rate = median(slopes)
        if alert.sign * rate <= alert.epsilon:
            return False
        if any(abs(slope - rate) > 0.25 * abs(rate) for slope in slopes):
            return False
        remaining = (alert.threshold - value) / rate
        return 0 < remaining <= self.lead_s

    def observe(self, sample: WatchSample) -> list[GameEvent]:
        events = []
        for alert in list(self.alerts.values()):
            value = sample.values.get(alert.quantity)
            if not sample.valid:
                alert.points.clear()
                alert.armed = False
                continue
            if sample.context != alert.context:
                self._remove(alert, "context_changed")
                continue
            if not _number(value) or not _number(sample.timestamp):
                alert.points.clear()
                alert.armed = False
                continue
            if not alert.points:
                self._baseline(alert, sample, value)
                continue
            dt = sample.timestamp - alert.points[-1][0]
            if dt == 0:
                continue  # The adapter has not acquired another sample yet.
            if dt < 0:
                self._baseline(alert, sample, value)
                continue
            alert.points.append((sample.timestamp, value))
            reached = alert.sign * (value - alert.threshold) >= 0
            if alert.armed and (reached or self._early(alert, sample, value)):
                events.append(self._event(alert, value))
                self._remove(alert, "fired")
                continue
            if alert.sign * (value - alert.threshold) < -alert.epsilon:
                alert.armed = True
            if not sample.predictable or not alert.predictable:
                alert.points.clear()
                alert.points.append((sample.timestamp, value))
            alert.predictable = sample.predictable
        return events

    @staticmethod
    def _event(alert: _Alert, value: float) -> GameEvent:
        return GameEvent(
            "alert",
            "critical",
            {
                "summary": f"Alert: {alert.description} reaching "
                f"{alert.requested_value:,.6g} {alert.requested_unit} ({alert.direction}), "
                "as the player asked. Announce briefly; do not claim it has already arrived.",
                # No current value: it is stale by the time a reply is spoken.
                "facts": [f"Label: {alert.label}"] if alert.label else [],
                "speak": alert.announcement,
                "ttl_s": 10,
                "alert_id": alert.id,
                "memory_salience": 0,
            },
            datetime.now(UTC),
        )

    async def run(self, submit) -> None:
        """Sleep without polling until a player creates an alert."""
        while True:
            await self._active.wait()
            try:
                for event in self.observe(self.adapter.watch_sample()):
                    submit(event)
            except Exception:
                _LOG.exception("Unable to sample telemetry alerts")
                for alert in self.alerts.values():
                    alert.points.clear()
                    alert.armed = False
            await asyncio.sleep(0.5)
