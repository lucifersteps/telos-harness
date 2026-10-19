"""MCP config loading."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    transport: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    bearer_token: str = ""
    bearer_token_env_var: str = ""
    oauth: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


def load_mcp_config(path: Path) -> dict[str, McpServerConfig]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    servers = data.get("mcpServers") if isinstance(data, dict) else {}
    if not isinstance(servers, dict):
        return {}
    return mcp_config_from_mapping(servers)


def mcp_config_from_mapping(servers: dict[str, Any]) -> dict[str, McpServerConfig]:
    return {
        str(name): _server_config_from_raw(str(name), raw)
        for name, raw in servers.items()
    }


def _server_config_from_raw(name: str, raw: Any) -> McpServerConfig:
    if not isinstance(raw, dict):
        return McpServerConfig(name=name, raw={})
    transport = str(raw.get("transport") or "").strip()
    if not transport:
        transport = "http" if str(raw.get("url") or "").strip() else "stdio"
    return McpServerConfig(
        name=name,
        transport=transport,
        command=str(raw.get("command") or ""),
        args=[str(item) for item in list(raw.get("args") or [])],
        env={str(key): str(value) for key, value in dict(raw.get("env") or {}).items()},
        url=str(raw.get("url") or ""),
        headers={str(key): str(value) for key, value in dict(raw.get("headers") or {}).items()},
        bearer_token=str(raw.get("bearerToken") or raw.get("bearer_token") or ""),
        bearer_token_env_var=str(
            raw.get("bearerTokenEnvVar") or raw.get("bearer_token_env_var") or ""
        ),
        oauth=dict(raw.get("oauth") or {}) if isinstance(raw.get("oauth"), dict) else {},
        raw=dict(raw),
    )
