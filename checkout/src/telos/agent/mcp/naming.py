"""MCP tool naming helpers."""

from __future__ import annotations


MCP_PREFIX = "mcp__"


def qualify_mcp_tool_name(server_name: str, tool_name: str) -> str:
    return f"{MCP_PREFIX}{_clean(server_name)}__{_clean(tool_name)}"


def is_mcp_tool_name(name: str) -> bool:
    return name.startswith(MCP_PREFIX)


def split_mcp_tool_name(name: str) -> tuple[str, str]:
    if not is_mcp_tool_name(name):
        raise ValueError(f"Not an MCP tool name: {name}")
    rest = name[len(MCP_PREFIX) :]
    server_name, separator, tool_name = rest.partition("__")
    if not separator or not server_name or not tool_name:
        raise ValueError(f"Malformed MCP tool name: {name}")
    return server_name, tool_name


def _clean(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in value)
    return cleaned.strip("_") or "unnamed"
