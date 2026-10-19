"""MCP server lifecycle manager."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any

from telos.agent.mcp.config_loader import McpServerConfig
from telos.agent.mcp.http_client import HttpMcpClient
from telos.agent.mcp.naming import qualify_mcp_tool_name
from telos.agent.mcp.oauth import McpAuthError, McpOAuthAuthorizationFlow, McpOAuthService
from telos.agent.mcp.stdio_client import McpProtocolError, McpTransportError, StdioMcpClient
from telos.agent.mcp.tool_wrapper import wrap_mcp_tool
from telos.agent.tools.types import ExecutableTool, create_executable_tool


@dataclass(frozen=True)
class McpToolInfo:
    name: str
    description: str = ""
    parameters: dict[str, Any] | None = None


@dataclass(frozen=True)
class McpRegistrationResult:
    registered: list[str] = field(default_factory=list)
    collisions: list[str] = field(default_factory=list)


class McpToolCallInterruptedError(McpProtocolError):
    """Raised when an MCP tool call may have executed but the transport died."""

    def __init__(
        self,
        *,
        server_name: str,
        tool_name: str,
        cause: BaseException,
        reconnected: bool,
    ):
        self.server_name = server_name
        self.tool_name = tool_name
        self.cause = cause
        self.reconnected = reconnected
        super().__init__(
            (
                f"MCP tool call {qualify_mcp_tool_name(server_name, tool_name)!r} was interrupted "
                "after Telos handed it to the transport; execution status is unknown. "
                f"Connection reconnected: {str(reconnected).lower()}."
            )
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "server_name": self.server_name,
            "tool_name": self.tool_name,
            "qualified_tool_name": qualify_mcp_tool_name(self.server_name, self.tool_name),
            "status": "unknown",
            "reconnected": self.reconnected,
            "cause": str(self.cause),
        }


class McpConnectionManager:
    def __init__(self, *, oauth_service: McpOAuthService | None = None):
        self._tools: dict[str, tuple[str, McpToolInfo]] = {}
        self._clients: dict[str, StdioMcpClient | HttpMcpClient] = {}
        self._configs: dict[str, McpServerConfig] = {}
        self._auth_required_configs: dict[str, McpServerConfig] = {}
        self._oauth_flows: dict[str, tuple[str, McpOAuthAuthorizationFlow]] = {}
        self.oauth_service = oauth_service

    async def connect_server(self, config: McpServerConfig) -> McpRegistrationResult:
        self._configs[config.name] = config
        old_client = self._clients.pop(config.name, None)
        if old_client is not None:
            await old_client.close()
        self._tools = {
            name: value
            for name, value in self._tools.items()
            if value[0] != config.name
        }
        self._auth_required_configs.pop(config.name, None)
        if self._should_register_auth_tool(config):
            self._auth_required_configs[config.name] = config
            return McpRegistrationResult(
                registered=[qualify_mcp_tool_name(config.name, "authenticate")]
            )
        client = self._client_for_config(config)
        try:
            await client.start()
            raw_tools = await client.list_tools()
        except Exception:
            await client.close()
            raise
        tools = [
            McpToolInfo(
                name=str(item.get("name") or ""),
                description=str(item.get("description") or ""),
                parameters=dict(item.get("inputSchema") or {"type": "object"}),
            )
            for item in raw_tools
            if str(item.get("name") or "").strip()
        ]
        result = self.register_tools(config.name, tools)
        self._clients[config.name] = client
        return result

    async def connect_all(
        self,
        configs: dict[str, McpServerConfig],
    ) -> dict[str, McpRegistrationResult]:
        results: dict[str, McpRegistrationResult] = {}
        for name, config in configs.items():
            results[name] = await self.connect_server(config)
        return results

    def register_tools(
        self,
        server_name: str,
        tools: list[McpToolInfo],
    ) -> McpRegistrationResult:
        registered: list[str] = []
        collisions: list[str] = []
        for tool in tools:
            qualified = qualify_mcp_tool_name(server_name, tool.name)
            if qualified in self._tools:
                collisions.append(qualified)
                continue
            self._tools[qualified] = (server_name, tool)
            registered.append(qualified)
        return McpRegistrationResult(registered=registered, collisions=collisions)

    def list_tool_names(self) -> list[str]:
        return sorted(
            [
                *self._tools,
                *(
                    qualify_mcp_tool_name(server_name, "authenticate")
                    for server_name in self._auth_required_configs
                ),
            ]
        )

    def executable_tools(self) -> list[ExecutableTool]:
        tools: list[ExecutableTool] = []
        for qualified in self.list_tool_names():
            if qualified not in self._tools:
                server_name = _server_name_for_auth_tool(qualified)
                if server_name:
                    config = self._auth_required_configs.get(server_name)
                    if config is not None:
                        tools.append(self._authenticate_tool(config))
                continue
            server_name, tool = self._tools[qualified]

            async def call_bound(
                name: str,
                arguments: dict[str, Any],
                *,
                bound_server: str = server_name,
            ) -> object:
                return await self._call_server_tool(bound_server, name, arguments)

            tools.append(
                wrap_mcp_tool(
                    server_name=server_name,
                    tool_name=tool.name,
                    description=tool.description,
                    parameters=tool.parameters or {"type": "object"},
                    call=call_bound,
                )
            )
        return tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> object:
        server_name = ""
        original_name = tool_name
        if tool_name.startswith("mcp__"):
            from telos.agent.mcp.naming import split_mcp_tool_name

            server_name, original_name = split_mcp_tool_name(tool_name)
        else:
            matches = [
                server
                for server, tool in self._tools.values()
                if tool.name == original_name
            ]
            if len(matches) == 1:
                server_name = matches[0]
        if not server_name:
            raise ValueError(f"MCP tool is not registered: {tool_name}")
        if original_name == "authenticate" and server_name in self._auth_required_configs:
            return await self._handle_authenticate_tool(server_name, arguments)
        return await self._call_server_tool(server_name, original_name, arguments)

    async def close_all(self) -> None:
        clients = list(self._clients.values())
        flows = list(self._oauth_flows.values())
        self._clients.clear()
        self._tools.clear()
        self._configs.clear()
        self._auth_required_configs.clear()
        self._oauth_flows.clear()
        for client in clients:
            await client.close()
        for _server_name, flow in flows:
            await flow.cancel()

    async def _call_server_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> object:
        client = await self._ensure_client_connected(server_name)
        try:
            return await client.call_tool(tool_name, arguments)
        except McpTransportError as error:
            reconnected = await self._recover_after_interrupted_call(server_name)
            raise McpToolCallInterruptedError(
                server_name=server_name,
                tool_name=tool_name,
                cause=error,
                reconnected=reconnected,
            ) from error

    async def _ensure_client_connected(
        self,
        server_name: str,
    ) -> StdioMcpClient | HttpMcpClient:
        client = self._clients.get(server_name)
        if client is not None and client.is_alive():
            return client
        config = self._configs.get(server_name)
        if config is None:
            raise ValueError(f"MCP server is not connected: {server_name}")
        await self.connect_server(config)
        client = self._clients.get(server_name)
        if client is None:
            raise ValueError(f"MCP server failed to reconnect: {server_name}")
        return client

    async def _recover_after_interrupted_call(self, server_name: str) -> bool:
        client = self._clients.pop(server_name, None)
        if client is not None:
            await client.close()
        config = self._configs.get(server_name)
        if config is None:
            return False
        try:
            await self.connect_server(config)
        except Exception:
            return False
        return self._clients.get(server_name) is not None

    def _client_for_config(self, config: McpServerConfig) -> StdioMcpClient | HttpMcpClient:
        transport = (config.transport or "").strip().lower()
        if transport == "http" or (transport == "stdio" and config.url and not config.command):
            auth_provider = None
            if (
                self.oauth_service is not None
                and config.oauth
                and not config.bearer_token
                and not config.bearer_token_env_var
                and self.oauth_service.has_tokens(config)
            ):
                auth_provider = self.oauth_service.provider_for_config(config)
            return HttpMcpClient(config, auth_provider=auth_provider)
        if transport == "stdio":
            return StdioMcpClient(config)
        raise ValueError(f"Unsupported MCP transport for {config.name!r}: {config.transport!r}")

    def _should_register_auth_tool(self, config: McpServerConfig) -> bool:
        transport = (config.transport or "").strip().lower()
        is_http = transport == "http" or (transport == "stdio" and config.url and not config.command)
        return (
            is_http
            and self.oauth_service is not None
            and bool(config.oauth)
            and not config.bearer_token
            and not config.bearer_token_env_var
            and not self.oauth_service.has_tokens(config)
        )

    def _authenticate_tool(self, config: McpServerConfig) -> ExecutableTool:
        qualified = qualify_mcp_tool_name(config.name, "authenticate")

        async def handler(args: dict[str, Any]) -> object:
            return await self._handle_authenticate_tool(config.name, args)

        return create_executable_tool(
            name=qualified,
            description=(
                f"Authenticate with MCP server {config.name!r} via OAuth. "
                "Use action=begin to get an authorization URL, then action=complete "
                "after the browser flow finishes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "begin, complete, or cancel",
                    },
                    "flow_id": {"type": "string"},
                    "timeout": {"type": "number"},
                },
                "required": [],
            },
            handler=handler,
            need_confirm=True,
        )

    async def _handle_authenticate_tool(
        self,
        server_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        config = self._auth_required_configs.get(server_name) or self._configs.get(server_name)
        if config is None:
            raise ValueError(f"MCP server is not configured for authentication: {server_name}")
        if self.oauth_service is None:
            raise McpAuthError("MCP OAuth service is not configured")
        action = str(arguments.get("action") or "begin").strip().lower()
        if action == "begin":
            return await self._begin_authentication(server_name, config)
        if action == "complete":
            flow_id = self._resolve_flow_id(server_name, str(arguments.get("flow_id") or ""))
            timeout = float(arguments.get("timeout") or 900)
            return await self._complete_authentication(server_name, config, flow_id, timeout)
        if action == "cancel":
            flow_id = self._resolve_flow_id(server_name, str(arguments.get("flow_id") or ""))
            _server_name, flow = self._oauth_flows.pop(flow_id)
            await flow.cancel()
            return {"status": "cancelled", "server_name": server_name, "flow_id": flow_id}
        raise ValueError("action must be begin, complete, or cancel")

    async def _begin_authentication(
        self,
        server_name: str,
        config: McpServerConfig,
    ) -> dict[str, Any]:
        flow = await self.oauth_service.begin_authorization(config)
        flow_id = secrets.token_urlsafe(16)
        self._oauth_flows[flow_id] = (server_name, flow)
        return {
            "status": "pending",
            "server_name": server_name,
            "flow_id": flow_id,
            "authorization_url": flow.authorization_url,
            "redirect_uri": flow.redirect_uri,
            "message": "Open authorization_url in a browser, finish login, then call this tool with action=complete and the same flow_id.",
        }

    async def _complete_authentication(
        self,
        server_name: str,
        config: McpServerConfig,
        flow_id: str,
        timeout: float,
    ) -> dict[str, Any]:
        _server_name, flow = self._oauth_flows.pop(flow_id)
        tokens = await flow.complete(timeout=timeout)
        result = await self.connect_server(config)
        return {
            "status": "authorized",
            "server_name": server_name,
            "flow_id": flow_id,
            "registered": result.registered,
            "collisions": result.collisions,
            "access_token_saved": bool(tokens.access_token),
        }

    def _resolve_flow_id(self, server_name: str, flow_id: str) -> str:
        if flow_id:
            if flow_id not in self._oauth_flows:
                raise ValueError(f"MCP OAuth flow not found: {flow_id}")
            return flow_id
        matches = [
            candidate_id
            for candidate_id, (candidate_server, _flow) in self._oauth_flows.items()
            if candidate_server == server_name
        ]
        if len(matches) != 1:
            raise ValueError("flow_id is required when there is no single pending flow")
        return matches[0]


def _server_name_for_auth_tool(qualified: str) -> str:
    if not qualified.startswith("mcp__") or not qualified.endswith("__authenticate"):
        return ""
    return qualified[len("mcp__") : -len("__authenticate")]
