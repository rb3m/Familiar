"""Core tool specifications, validation and exception-safe dispatch."""

import json
import math
from collections.abc import Awaitable, Callable, Collection

from familiar.crew import CONTROLS, ROLES
from familiar.interfaces import ToolCall, ToolSpec

ToolHandler = Callable[..., Awaitable[dict]]


def _validate(value: object, schema: dict, path: str = "arguments") -> None:
    kind = schema.get("type")
    matches = {
        "object": isinstance(value, dict),
        "string": isinstance(value, str),
        "number": type(value) in {int, float} and math.isfinite(value),
        "integer": type(value) is int,
        "boolean": type(value) is bool,
        "array": isinstance(value, list),
        "null": value is None,
    }
    if kind is not None and not matches.get(kind, False):
        raise ValueError(f"{path} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} must be one of {schema['enum']}")
    _validate_bounds(value, schema, path)
    if not isinstance(value, dict):
        return
    missing = set(schema.get("required", ())) - value.keys()
    if missing:
        raise ValueError(f"{path} missing required fields: {', '.join(sorted(missing))}")
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False and value.keys() - properties.keys():
        raise ValueError(f"{path} contains unknown fields")
    for key, item in value.items():
        if key in properties:
            _validate(item, properties[key], f"{path}.{key}")


def validate_arguments(arguments: object, schema: dict) -> None:
    """Raise ValueError when tool arguments do not match a JSON-schema subset."""
    _validate(arguments, schema)


def _validate_bounds(value: object, schema: dict, path: str) -> None:
    if type(value) not in {int, float}:
        return
    if value < schema.get("minimum", float("-inf")):
        raise ValueError(f"{path} is below minimum")
    if value > schema.get("maximum", float("inf")):
        raise ValueError(f"{path} exceeds maximum")


def _role(name: str, carrier_only: bool, role: str | None) -> str | None:
    """carrier_only predates roles and means the controls role (ADR 0018)."""
    if role is not None and role not in ROLES:
        raise ValueError(f"Tool {name}: unknown role {role!r}")
    if carrier_only and role not in (None, CONTROLS):
        raise ValueError(f"Tool {name}: carrier_only conflicts with role {role!r}")
    if carrier_only:
        return CONTROLS
    return role


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolSpec, ToolHandler]] = {}
        self._roles: dict[str, str] = {}
        self._autonomous: set[str] = set()

    def register(
        self,
        spec: ToolSpec,
        handler: ToolHandler,
        *,
        carrier_only: bool = False,
        role: str | None = None,
        autonomous: bool = False,
    ) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Duplicate tool: {spec.name}")
        role = _role(spec.name, carrier_only, role)
        self._tools[spec.name] = spec, handler
        if role is not None:
            self._roles[spec.name] = role
        if autonomous:
            self._autonomous.add(spec.name)

    def role_of(self, name: str) -> str | None:
        return self._roles.get(name)

    @property
    def roles_in_use(self) -> frozenset[str]:
        """Roles that own at least one registered tool."""
        return frozenset(self._roles.values())

    def offered(self, names, roles: Collection[str] | None = None) -> list[ToolSpec]:
        """Tools an event may offer an unprompted turn: named, registered as autonomous."""
        allowed = set(names) & self._autonomous
        return [spec for spec in self.specs(roles) if spec.name in allowed]

    def specs(self, roles: Collection[str] | None = None) -> list[ToolSpec]:
        """Tools open to everyone plus those of the given roles; None means every tool."""
        return [
            spec
            for spec, _ in self._tools.values()
            if roles is None or self._roles.get(spec.name, None) in {None, *roles}
        ]

    async def execute(self, call: ToolCall) -> dict:
        try:
            entry = self._tools.get(call.name)
            if entry is None:
                return {"error": f"Unknown tool: {call.name}"}
            spec, handler = entry
            _validate(call.arguments, spec.parameters)
            result = await handler(**call.arguments)
            if not isinstance(result, dict):
                raise ValueError("Tool result must be an object")
            json.dumps(result, allow_nan=False)
            return result
        except Exception as exc:
            return {"error": str(exc) or type(exc).__name__}
