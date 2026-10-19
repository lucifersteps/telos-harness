"""MCP tool wrapper."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from telos.agent.mcp.naming import qualify_mcp_tool_name
from telos.agent.tools.types import ExecutableTool, create_executable_tool


McpCall = Callable[[str, dict[str, Any]], Awaitable[object] | object]


def wrap_mcp_tool(
    *,
    server_name: str,
    tool_name: str,
    description: str,
    parameters: dict[str, Any],
    call: McpCall,
) -> ExecutableTool:
    qualified = qualify_mcp_tool_name(server_name, tool_name)

    async def handler(args: dict[str, Any]) -> object:
        result = call(tool_name, args)
        if hasattr(result, "__await__"):
            return await result  # type: ignore[no-any-return]
        return result

    return create_executable_tool(
        name=qualified,
        description=description,
        parameters=parameters,
        handler=handler,
        need_confirm=True,
    )
