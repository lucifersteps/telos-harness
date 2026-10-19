"""Helpers for connecting configured MCP servers."""

from __future__ import annotations

from telos.agent.mcp.config_loader import mcp_config_from_mapping
from telos.agent.mcp.connection_manager import McpConnectionManager
from telos.core.settings import TelosSettings


def mcp_tools_enabled(enabled_tools: list[str]) -> bool:
    return "*" in enabled_tools or any(item.startswith("mcp__") for item in enabled_tools)


async def connect_configured_mcp(settings: TelosSettings) -> McpConnectionManager | None:
    if not settings.mcp.enabled:
        return None
    if not mcp_tools_enabled(settings.tools.enabled):
        return None
    configs = mcp_config_from_mapping(settings.mcp.servers)
    if not configs:
        return None
    manager = McpConnectionManager()
    await manager.connect_all(configs)
    return manager
