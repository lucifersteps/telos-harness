"""Minimal stdio MCP JSON-RPC client."""

from __future__ import annotations

import asyncio
import json
import os
from asyncio.subprocess import Process
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telos.agent.mcp.config_loader import McpServerConfig


class McpProtocolError(RuntimeError):
    """Raised when an MCP server returns an invalid JSON-RPC response."""


class McpTransportError(McpProtocolError):
    """Raised when the MCP transport closes or times out."""


@dataclass(frozen=True)
class McpCallResult:
    raw: dict[str, Any]

    @property
    def result(self) -> dict[str, Any]:
        return dict(self.raw.get("result") or {})


class StdioMcpClient:
    def __init__(self, config: McpServerConfig, *, request_timeout: float = 10.0):
        self.config = config
        self.request_timeout = request_timeout
        self.process: Process | None = None
        self._next_id = 1

    async def start(self) -> None:
        if self.process is not None:
            return
        if not self.config.command.strip():
            raise McpProtocolError(f"MCP server {self.config.name!r} has no command")
        env = dict(os.environ)
        env.update(self.config.env)
        cwd_raw = str(self.config.raw.get("cwd") or "").strip()
        self.process = await asyncio.create_subprocess_exec(
            self.config.command,
            *self.config.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(Path(cwd_raw).expanduser()) if cwd_raw else None,
        )
        await self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "telos", "version": "0.1.0"},
            },
        )
        await self.notify("notifications/initialized", {})

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
        await self._write_json(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": dict(params or {}),
            }
        )
        while True:
            message = await self._read_json()
            if message.get("id") != request_id:
                # Ignore server notifications and unrelated responses.
                continue
            if "error" in message:
                raise McpProtocolError(f"MCP request {method} failed: {message['error']}")
            if "result" not in message:
                raise McpProtocolError(f"MCP request {method} missing result")
            return McpCallResult(raw=message)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._require_process()
        await self._write_json(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": dict(params or {}),
            }
        )

    async def close(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except BrokenPipeError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=1.0)
            except TimeoutError:
                process.kill()
                await process.wait()

    def _require_process(self) -> Process:
        if self.process is None:
            raise McpTransportError("MCP client is not started")
        return self.process

    def is_alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def _write_json(self, payload: dict[str, Any]) -> None:
        process = self._require_process()
        if process.stdin is None:
            raise McpTransportError("MCP process stdin is not available")
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            process.stdin.write(data + b"\n")
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as error:
            raise McpTransportError(f"MCP process write failed: {error}") from error

    async def _read_json(self) -> dict[str, Any]:
        process = self._require_process()
        if process.stdout is None:
            raise McpTransportError("MCP process stdout is not available")
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=self.request_timeout)
        except TimeoutError as error:
            raise McpTransportError("MCP process timed out waiting for response") from error
        if not line:
            raise McpTransportError("MCP process closed stdout")
        try:
            message = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as error:
            raise McpProtocolError(f"Invalid MCP JSON response: {line!r}") from error
        if not isinstance(message, dict):
            raise McpProtocolError("MCP response must be a JSON object")
        return message
