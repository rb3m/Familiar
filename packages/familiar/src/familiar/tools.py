"""Core tool specifications, validation and exception-safe dispatch."""

import json
import math
from collections.abc import Awaitable, Callable

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


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolSpec, ToolHandler]] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Duplicate tool: {spec.name}")
        self._tools[spec.name] = spec, handler

    def specs(self) -> list[ToolSpec]:
        return [spec for spec, _ in self._tools.values()]

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
