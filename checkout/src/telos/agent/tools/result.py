"""Tool result normalization."""

from __future__ import annotations

import json

from telos.agent.tools.types import ToolResult


def normalize_tool_result(value: object) -> ToolResult:
    if isinstance(value, ToolResult):
        return value
    failure = _failure_payload(value)
    if failure is None:
        return ToolResult(output=value)
    return ToolResult(
        output=value,
        is_error=True,
        error=failure["error"],
        retryable=failure["retryable"],
    )


def render_tool_output(result: ToolResult) -> object:
    output = result.output
    if output is None:
        return ""
    if _is_content_parts(output):
        return output
    if isinstance(output, str):
        return output
    try:
        return json.dumps(output, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(output)


def error_tool_result(
    message: str,
    *,
    stop_turn: bool = False,
    retryable: bool = False,
) -> ToolResult:
    return ToolResult(
        output=message,
        is_error=True,
        stop_turn=stop_turn,
        error=message,
        retryable=retryable,
    )


def _failure_payload(value: object) -> dict[str, object] | None:
    payload = value
    if isinstance(value, str):
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    if payload.get("ok") is not False:
        return None
    return {
        "error": str(payload.get("error") or payload.get("message") or "工具返回 ok=false"),
        "retryable": bool(payload.get("retryable")),
    }


def _is_content_parts(value: object) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        if str(item.get("type") or "") not in {
            "text",
            "image_file",
            "video_file",
            "image_url",
            "video_url",
        }:
            return False
    return True


def input_error_result(message: str, *, code: str = "invalid_arguments") -> ToolResult:
    """Only parsing, schema validation and explicitly safe resolvers use this."""
    return ToolResult(output={"status": "input_error", "code": code,
        "execution_started": False, "message": message}, is_error=True, error=message)
