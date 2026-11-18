"""Commander and ship state reconstructed from known journal events."""

from dataclasses import asdict, dataclass, field

from familiar.interfaces import load_data


@dataclass
class EliteState:
    commander: str | None = None
    ship: str | None = None
    ship_id: int | None = None
    ship_name: str | None = None
    ship_ident: str | None = None
    cargo_capacity: int | None = None
    shields_fitted: bool | None = None
    timestamp: str | None = None
    government: str | None = None
    station_type: str | None = None
    station_services: list | None = None
    station_economy: str | None = None
    max_jump_range: float | None = None
    fuel_capacity: float | None = None
    fuel_level: float | None = None
    system: str | None = None
    star_pos: list | None = None
    security: str | None = None
    economy: str | None = None
    population: int | None = None
    allegiance: str | None = None
    faction: dict | None = None
    star_class: str | None = None
    body: str | None = None
    station: str | None = None
    docked: bool | None = None
    supercruise: bool | None = None
    docking: str | None = None
    docking_reason: str | None = None
    landing_pad: int | None = None
    docking_event_count: int = 0
    last_docking_event: dict = field(default_factory=dict)
    interdicted: bool = False
    hull: float | None = None
    shields_up: bool | None = None
    bodies: int | None = None
    scans: dict[str, dict] = field(default_factory=dict)
    alive: bool = True
    shutdown: bool = False

    def to_dict(self):
        return asdict(self)

    def _copy(self, event, fields):
        for source, target in fields.items():
            if source in event:
                setattr(self, target, event[source])

    def apply(self, event: dict):
        kind = event.get("event")
        if kind == "LoadGame":
            self.__dict__.update(EliteState().__dict__)
        if event.get("timestamp"):
            self.timestamp = max(self.timestamp or "", event["timestamp"])
        match kind:
            case "LoadGame" | "Loadout":
                self._copy(
                    event,
                    {
                        "Commander": "commander",
                        "Ship": "ship",
                        "ShipID": "ship_id",
                        "ShipName": "ship_name",
                        "ShipIdent": "ship_ident",
                        "CargoCapacity": "cargo_capacity",
                        "MaxJumpRange": "max_jump_range",
                        "FuelLevel": "fuel_level",
                        "HullHealth": "hull",
                    },
                )
                if kind == "Loadout":
                    self._loadout(event)
                capacity = event.get("FuelCapacity", self.fuel_capacity)
                self.fuel_capacity = (
                    capacity.get("Main") if isinstance(capacity, dict) else capacity
                )
                self.shutdown, self.alive = False, True
            case "Location" | "FSDJump":
                self._copy(
                    event,
                    {
                        "StarSystem": "system",
                        "StarPos": "star_pos",
                        "SystemSecurity_Localised": "security",
                        "SystemEconomy_Localised": "economy",
                        "Population": "population",
                        "SystemAllegiance": "allegiance",
                        "SystemFaction": "faction",
                        "FuelLevel": "fuel_level",
                    },
                )
                self.government = event.get("SystemGovernment_Localised")
                self._station(event)
                self.security = event.get("SystemSecurity_Localised", event.get("SystemSecurity"))
                self.economy = event.get("SystemEconomy_Localised", event.get("SystemEconomy"))
                self.population = event.get("Population")
                self.allegiance = event.get("SystemAllegiance")
                self.faction = event.get("SystemFaction")
                self.bodies, self.scans = None, {}
                self.station, self.body = event.get("StationName"), event.get("Body")
                self.docked = event.get("Docked", False)
                if kind == "FSDJump":
                    self.supercruise = True
            case "StartJump":
                self.star_class = event.get("StarClass")
            case "SupercruiseEntry" | "SupercruiseExit":
                self.supercruise = kind == "SupercruiseEntry"
                self.body = event.get("Body")
            case "ApproachBody":
                self.body = event.get("Body")
            case "Docked" | "Undocked":
                self.docked = kind == "Docked"
                self.station = event.get("StationName")
                self.docking = kind
                self._station(event)
            case (
                "DockingRequested"
                | "DockingGranted"
                | "DockingDenied"
                | "DockingTimeout"
                | "DockingCancelled"
            ):
                self.last_docking_event = dict(event)
                self.docking_event_count += 1
                self.docking = kind
                self.docking_reason = event.get("Reason")
                self.station = event.get("StationName", self.station)
                self.landing_pad = event.get("LandingPad")
            case "Interdicted" | "EscapeInterdiction":
                self.interdicted = kind == "Interdicted"
            case "HullDamage":
                self.hull = event.get("Health")
            case "ShieldState":
                self.shields_up = event.get("ShieldsUp")
            case "FuelScoop":
                self.fuel_level = event.get("Total")
            case "FSSDiscoveryScan":
                self.bodies = event.get("BodyCount")
            case "Scan":
                self.scans[event.get("BodyName", "unknown")] = {
                    key: event[key]
                    for key in (
                        "BodyName",
                        "StarType",
                        "PlanetClass",
                        "WasDiscovered",
                        "WasMapped",
                        "Landable",
                        "TerraformState",
                        "Rings",
                        "Atmosphere",
                    )
                    if key in event
                }
            case "Died":
                self.alive = False
            case "Resurrect":
                self.alive = True
            case "Shutdown":
                self.shutdown = True

    def _loadout(self, event):
        if "Modules" not in event:
            self.shields_fitted = None
            return
        pattern = load_data("familiar_elite", "interest").value["shield_generator_pattern"]
        self.shields_fitted = any(
            pattern.casefold() in module.get("Item", "").casefold() for module in event["Modules"]
        )

    def _station(self, event):
        self.station_type = event.get("StationType")
        self.station_services = event.get("StationServices")
        self.station_economy = event.get("StationEconomy_Localised")
