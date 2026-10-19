"""Minimal HTTP MCP JSON-RPC client."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from telos.agent.mcp.config_loader import McpServerConfig
from telos.agent.mcp.oauth import McpAuthProvider, McpBearerTokenProvider
from telos.agent.mcp.stdio_client import McpCallResult, McpProtocolError, McpTransportError


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: str


class McpHttpUnauthorizedError(McpProtocolError):
    """MCP HTTP server rejected current credentials."""


class McpHttpTransportError(McpTransportError):
    """MCP HTTP transport failed before a JSON-RPC response was available."""


class HttpMcpClient:
    def __init__(
        self,
        config: McpServerConfig,
        *,
        request_timeout: float = 10.0,
        auth_provider: McpAuthProvider | None = None,
    ):
        self.config = config
        self.request_timeout = request_timeout
        self.auth_provider = auth_provider or McpBearerTokenProvider.from_config(config)
        self._next_id = 1
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        if not self.config.url.strip():
            raise McpProtocolError(f"MCP HTTP server {self.config.name!r} has no url")
        await self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "telos", "version": "0.1.0"},
            },
        )
        await self.notify("notifications/initialized", {})
        self._started = True

    async def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor = ""
        while True:
            params = {"cursor": cursor} if cursor else {}
            response = await self.request("tools/list", params)
            result = response.result
            raw_tools = result.get("tools") or []
            if not isinstance(raw_tools, list):
                raise McpProtocolError("MCP tools/list result.tools must be a list")
            tools.extend(dict(item) for item in raw_tools if isinstance(item, dict))
            next_cursor = str(result.get("nextCursor") or "")
            if not next_cursor:
                break
            cursor = next_cursor
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = await self.request(
            "tools/call",
            {"name": name, "arguments": dict(arguments or {})},
        )
        return response.result

    async def request(self, method: str, params: dict[str, Any] | None = None) -> McpCallResult:
        request_id = self._next_id
        self._next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": dict(params or {}),
        }
        response = await self._post(payload)
        message = _parse_json_response(response, method)
        if message.get("id") != request_id:
            raise McpProtocolError(f"MCP request {method} returned mismatched id")
        if "error" in message:
            raise McpProtocolError(f"MCP request {method} failed: {message['error']}")
        if "result" not in message:
            raise McpProtocolError(f"MCP request {method} missing result")
        return McpCallResult(raw=message)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": dict(params or {}),
        }
        await self._post(payload, expect_body=False)

    async def close(self) -> None:
        self._started = False

    def is_alive(self) -> bool:
        return self._started

    async def _post(
        self,
        payload: dict[str, Any],
        *,
        expect_body: bool = True,
    ) -> HttpResponse:
        headers = self.auth_provider.headers()
        try:
            return await asyncio.to_thread(
                _post_json,
                self.config.url,
                payload,
                headers,
                self.request_timeout,
                expect_body,
            )
        except McpHttpUnauthorizedError:
            self.auth_provider.invalidate()
            raise


def _post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
    expect_body: bool,
) -> HttpResponse:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "content-type": "application/json",
            "accept": "application/json",
            **headers,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read().decode("utf-8")
            return HttpResponse(status=int(response.status), body=data)
    except urllib.error.HTTPError as error:
        data = error.read().decode("utf-8", errors="replace")
        if error.code in {401, 403}:
            raise McpHttpUnauthorizedError(
                f"MCP HTTP credentials were rejected: {error.code} {data}"
            ) from error
        raise McpHttpTransportError(f"MCP HTTP request failed: {error.code} {data}") from error
    except Exception as error:  # noqa: BLE001 - normalize transport boundary errors.
        raise McpHttpTransportError(f"MCP HTTP request failed: {error}") from error


def _parse_json_response(response: HttpResponse, method: str) -> dict[str, Any]:
    if not response.body.strip():
        raise McpProtocolError(f"MCP request {method} returned empty response")
    try:
        parsed = json.loads(response.body)
    except json.JSONDecodeError as error:
        raise McpProtocolError(f"Invalid MCP HTTP JSON response: {response.body!r}") from error
    if not isinstance(parsed, dict):
        raise McpProtocolError("MCP HTTP response must be a JSON object")
    return parsed
