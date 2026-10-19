"""Tool argument validation."""

from __future__ import annotations

from telos.agent.tools.types import ExecutableTool
from telos.core.jsonschema import validate_json_schema


def validate_tool_args(tool: ExecutableTool, args: object) -> str | None:
    if not isinstance(args, dict):
        return "arguments must be a JSON object"
    schema = tool.parameters or {}
    if schema.get("type", "object") != "object":
        return None
    return validate_json_schema(schema, args)
