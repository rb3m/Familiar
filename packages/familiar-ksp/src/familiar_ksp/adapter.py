"""Kerbal Space Program game adapter."""

from familiar.interfaces import PLUGIN_API_VERSION

from .events import GROUNDED, EventDetector
from .quotes import attach_flavor, load_quotes
from .telemetry import STAGE_RESOURCES, KrpcTelemetry, ReplayTelemetry, Snapshot
from .units import distance, duration, number, resource


class KspAdapter:
    game = "ksp"
    name = "ksp"
    display_name = "Kerbal Space Program"
    icon = "🚀"
    api_version = PLUGIN_API_VERSION
    capabilities = frozenset({"events", "snapshot"})

    def __init__(self, settings: dict) -> None:
        self.latest = Snapshot()
        self.detector = EventDetector()
        self.quotes = load_quotes(settings.get("quotes_path"))
        self.source = self._source(settings)

    @staticmethod
    def _source(settings):
        if settings.get("replay"):
            return ReplayTelemetry(settings["replay"], settings.get("replay_speed", 1.0))
        return KrpcTelemetry(settings)

    async def events(self):
        try:
            async for snapshot in self.source:
                self.latest = snapshot
                for event in self.detector.feed(snapshot):
                    attach_flavor(event, self.quotes)
                    yield event
        finally:
            await self.source.aclose()

    async def snapshot(self) -> dict:
        result = self.latest.to_dict()
        result.update(paused=bool(self.latest.paused), driving=self.latest.in_flight)
        return result

    def describe(self) -> str:
        s = self.latest
        if not s.connected:
            return ""
        if not s.in_flight:
            return "Not in flight"
        fuels = "; ".join(
            f"{name}: {resource(getattr(s, key), getattr(s, key + '_max'))}"
            for key, name in STAGE_RESOURCES.items()
        )
        return "\n".join(
            [
                f"Vessel: {s.vessel_name} ({s.vessel_type or 'unknown'})",
                f"Location: {s.body or 'unknown'} / {s.biome or 'unknown'} / "
                f"{s.situation or 'unknown'}",
                f"Altitude: {distance(s.altitude_m)} mean; "
                f"{distance(s.surface_altitude_m)} surface",
                *self._motion_lines(),
                f"Stage: {number(s.current_stage, decimals=0)}; "
                f"parts: {number(s.part_count, decimals=0)}",
                f"Stage resources (KSP units): {fuels}",
                f"Delta-v: next stage {number(s.stage_delta_v_ms, ' m/s', decimals=0)}; "
                f"whole vessel {number(s.total_delta_v_ms, ' m/s', decimals=0)}",
                "Liquid fuel, whole vessel (KSP units): "
                f"{resource(s.liquid_fuel, s.liquid_fuel_max)}",
                "Electric charge (KSP units): "
                f"{resource(s.electric_charge, s.electric_charge_max)}",
                f"Crew: {', '.join(s.crew) or 'none'}; "
                f"capacity {number(s.crew_capacity, decimals=0)}",
                f"MET: {duration(s.met_s)}",
                f"Warp: {number(s.warp_rate, 'x')} ({s.warp_mode or 'unknown'})",
            ]
        )

    def _motion_lines(self) -> list[str]:
        s = self.latest
        if s.situation in GROUNDED:
            return [
                "Speed: stationary on the ground"
                if not s.speed_surface_ms
                else f"Speed: surface {number(s.speed_surface_ms, ' m/s')}"
            ]
        return [
            f"Speed: orbital {number(s.speed_orbital_ms, ' m/s')}; "
            f"surface {number(s.speed_surface_ms, ' m/s')}; "
            f"vertical {number(s.vertical_speed_ms, ' m/s')}",
            f"Ap: {distance(s.apoapsis_alt_m)} in {duration(s.time_to_apoapsis_s)}; "
            f"Pe: {distance(s.periapsis_alt_m)} in {duration(s.time_to_periapsis_s)}",
            f"Inclination: {number(s.inclination_deg, ' degrees')}",
        ]

    def whereabouts(self) -> dict | None:
        s = self.latest
        if not s.in_flight or not s.body:
            return None
        return dict(
            place_key=f"{s.body}/{s.biome or s.situation}",
            place=s.biome or s.body,
            region=s.body,
            activity=f"flying the {s.vessel_name} with {', '.join(s.crew) or 'no crew'}",
        )

    def tools(self) -> list:
        return []

    async def aclose(self) -> None:
        await self.source.aclose()
