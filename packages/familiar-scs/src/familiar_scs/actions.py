"""SCS action descriptions and argument schemas; execution lives in the adapter."""

from familiar.interfaces import ActionSpec

DESCRIPTIONS = {
    "light_cycle": "Cycle exterior lights once (state is not observable).",
    "high_beams": "Toggle high beams.",
    "beacon": "Toggle the beacon.",
    "hazards": "Toggle hazard warning lights.",
    "flash_headlights": "Flash the headlights.",
    "horn": "Sound the horn briefly.",
    "air_horn": "Sound the air horn briefly.",
    "blinker": "Toggle the left or right turn signal.",
    "wipers": "Cycle wipers once and report whether they are running.",
    "engine": "Set engine on or off; switching off requires confirmation.",
    "parking_brake": "Set parking brake on or off; applying it requires confirmation.",
    "cruise_control": "Set cruise control on or off.",
    "retarder": "Increase or decrease retarder by one step.",
    "lift_axle": "Toggle the truck lift axle.",
    "trailer_attach": "Toggle trailer coupling; this can detach the trailer.",
    "radio": "Toggle radio power without opening the radio screen.",
    "radio_station": "Select the next or previous radio station.",
    "quick_save": "Quick-save the game.",
    "screenshot": "Take a game screenshot.",
}
ARGUMENTS = {
    "blinker": ("side", ["left", "right"]),
    "engine": ("state", ["on", "off"]),
    "parking_brake": ("state", ["on", "off"]),
    "cruise_control": ("state", ["on", "off"]),
    "retarder": ("direction", ["up", "down"]),
    "radio_station": ("direction", ["next", "previous"]),
}
STATE_FIELDS = {
    "engine": "engine_on",
    "parking_brake": "park_brake_on",
    "cruise_control": "cruise_control_on",
}


def action_specs() -> dict[str, ActionSpec]:
    result = {}
    for name, description in DESCRIPTIONS.items():
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
        if name in ARGUMENTS:
            argument, choices = ARGUMENTS[name]
            schema["properties"][argument] = {"type": "string", "enum": choices}
            schema["required"] = [argument]
        result[name] = ActionSpec(
            description,
            schema,
            risky=name == "trailer_attach",
            risky_when={
                "engine": lambda args: args.get("state") == "off",
                "parking_brake": lambda args: args.get("state") == "on",
            }.get(name),
        )
    return result
