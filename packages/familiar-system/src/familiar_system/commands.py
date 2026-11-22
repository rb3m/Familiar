"""Inventory sensors so users can author their own allow-list overrides."""

import argparse

from familiar.interfaces import PLUGIN_API_VERSION

from familiar_system.sensors import match_rule, plausible, warning_line
from familiar_system.service import SystemService


def _inventory(reader) -> tuple:
    return reader.temperatures(), reader.partitions(), reader.resources().filesystems


def _display(value, unit: str = " °C") -> str:
    if value is None:
        return "-"
    return f"{value:g}{unit}"


def _matched_status(sensor, rule: dict) -> str:
    line, source = warning_line(sensor, rule)
    if not rule.get("alert") or line is None:
        return f"reported as {rule['role']} (no alerts)"
    return f"reported as {rule['role']}, alerts at {_display(line)} ({source})"


class SystemCommand:
    name = "system"
    api_version = PLUGIN_API_VERSION
    help = "Inspect system health sensors and filesystem selection"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("action", choices=["sensors"])

    async def run(self, args: argparse.Namespace, config) -> int:
        service = SystemService(config.system)
        try:
            return await self._run(service)
        finally:
            await service.aclose()

    async def _run(self, service: SystemService) -> int:
        if service.reader is None:
            print("System readers unavailable on this platform")
            return 1
        result = await service._read("inventory", lambda: _inventory(service.reader))
        if result is None:
            print("System inventory unavailable (read failed or timed out)")
            return 1
        temperatures, partitions, filesystems = result
        if not temperatures:
            print("Temperatures unavailable")
        for sensor in temperatures:
            self._sensor(sensor, service.settings)
        included = {fs.mountpoint for fs in filesystems}
        for partition in partitions:
            self._partition(partition, included, service.settings)
        return 0

    @staticmethod
    def _sensor(sensor, settings: dict) -> None:
        rule = match_rule(sensor, settings)
        if rule is None:
            status = "ignored (no rule)"
        if rule is not None and not plausible(sensor, settings):
            status = "ignored (implausible)"
        if rule is not None and plausible(sensor, settings):
            status = _matched_status(sensor, rule)
        print(
            f"{sensor.driver} | {sensor.device} | {sensor.label} | "
            f"{_display(sensor.celsius)} | max {_display(sensor.max_c)} | "
            f"crit {_display(sensor.crit_c)} | {status}"
        )

    @staticmethod
    def _partition(partition, included: set, settings: dict) -> None:
        status = "deduplicated"
        if partition.mountpoint in included:
            status = "included"
        if partition.fstype in settings["network_fstypes"]:
            status = "skipped network"
        print(f"{partition.mountpoint} | {partition.fstype} | {status}")
