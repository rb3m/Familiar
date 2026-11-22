"""Pure in-memory hysteresis, dwell and per-part cooldown."""

from dataclasses import dataclass
from datetime import datetime

from familiar.interfaces.types import GameEvent

from familiar_system.readers import Filesystem
from familiar_system.sensors import MatchedTemperature


@dataclass
class Latch:
    active: bool
    count: int = 0
    last_notice: float = float("-inf")


class AlertTracker:
    def __init__(self, settings: dict) -> None:
        self.settings = settings
        self.latches: dict[str, Latch] = {}

    def _crossed(self, key: str, over: bool, clear: bool, now: float) -> bool:
        latch = self.latches.get(key)
        if latch is None:
            self.latches[key] = Latch(active=over)
            return False
        if clear:
            latch.active = False
            latch.count = 0
            return False
        if latch.active:
            return False
        if not over:
            latch.count = 0
            return False
        latch.count += 1
        if latch.count < self.settings["dwell_samples"]:
            return False
        latch.active = True
        latch.count = 0
        if now - latch.last_notice < self.settings["cooldown_s"]:
            return False
        latch.last_notice = now
        return True

    def _thermal(self, item: MatchedTemperature, now: float) -> str | None:
        sensor, line = item.sensor, item.line_c
        if sensor.id is None or sensor.celsius is None or line is None:
            return None
        value = sensor.celsius
        crossed = self._crossed(
            f"thermal:{sensor.id}",
            value > line,
            value <= line - self.settings["clear_c"],
            now,
        )
        if not crossed:
            return None
        return f"{self._spoken(item)} is at {value:.0f} °C, over its {line:.0f} °C warning line."

    def _spoken(self, item) -> str:
        """A name to say aloud: "the CPU", "drive nvme1", not driver identifiers."""
        device = (item.sensor.device or item.sensor.label or "").split(" ")[0]
        template = self.settings.get("spoken_names", {}).get(item.role, item.role)
        return template.format(device=device)

    def _storage(self, filesystem: Filesystem, now: float) -> str | None:
        pct = filesystem.free_pct
        if filesystem.mountpoint is None or pct is None:
            return None
        crossed = self._crossed(
            f"storage:{filesystem.mountpoint}",
            pct < self.settings["free_alert_pct"],
            pct >= self.settings["free_rearm_pct"],
            now,
        )
        if not crossed:
            return None
        return (
            f"{filesystem.mountpoint} has {pct:g}% free ({filesystem.free_b / 1_000_000_000:g} GB)."
        )

    def sample(
        self,
        *,
        timestamp: datetime,
        temperatures: list[MatchedTemperature] = (),
        filesystems: list[Filesystem] = (),
        stale: bool = False,
    ) -> GameEvent | None:
        if stale:
            return None
        now = timestamp.timestamp()
        messages = [self._thermal(item, now) for item in temperatures]
        messages.extend(self._storage(item, now) for item in filesystems)
        summary = " ".join(filter(None, messages))
        if not summary:
            return None
        return GameEvent(
            kind="system_notice",
            priority="notable",
            timestamp=timestamp,
            data={
                "summary": f"System notice: {summary} Mention it briefly.",
                "ttl_s": self.settings["notice_ttl_s"],
                "companion_service": True,
            },
        )
