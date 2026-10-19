"""Web search tool."""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Callable
from typing import Any

from telos.agent.tools.types import ExecutableTool, create_executable_tool


SearchTransport = Callable[[dict[str, Any], str], dict[str, Any]]
DEFAULT_TAVILY_ENDPOINT = "https://api.tavily.com/search"


def create_web_search_tool(
    *,
    transport: SearchTransport | None = None,
) -> ExecutableTool:
    return create_executable_tool(
        name="web_search",
        description="搜索网页并返回带来源 URL 的简洁结果。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询。"},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 5,
                    "description": "最多返回的搜索结果数量，范围 1 到 10，默认 5。",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=lambda args: web_search(
            str(args["query"]),
            int(args.get("max_results") or 5),
            transport=transport,
        ),
    )


def web_search(
    query: str,
    max_results: int = 5,
    *,
    api_key: str | None = None,
    transport: SearchTransport | None = None,
) -> dict[str, Any]:
    if not str(query).strip():
        raise ValueError("Search query is required")
    limit = max(1, min(int(max_results), 10))
    resolved_api_key = api_key or os.environ.get("TAVILY_API_KEY", "").strip()
    if not resolved_api_key:
        raise ValueError("TAVILY_API_KEY is required for web_search")

    payload = {
        "query": str(query).strip(),
        "max_results": limit,
        "include_answer": True,
    }
    raw = transport(payload, resolved_api_key) if transport is not None else _send_tavily(payload, resolved_api_key)
    return _normalize_tavily_response(payload["query"], raw, limit)


def _send_tavily(payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    body = json.dumps({"api_key": api_key, **payload}).encode("utf-8")
    request = urllib.request.Request(
        DEFAULT_TAVILY_ENDPOINT,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read().decode("utf-8")
    except Exception as error:  # noqa: BLE001 - normalize tool boundary failures.
        raise ValueError(f"Tavily search failed: {error}") from error
    parsed = json.loads(data)
    if not isinstance(parsed, dict):
        raise ValueError("Tavily response must be a JSON object")
    return parsed


def _normalize_tavily_response(
    query: str,
    raw: dict[str, Any],
    max_results: int,
) -> dict[str, Any]:
    raw_results = raw.get("results") or []
    if not isinstance(raw_results, list):
        raise ValueError("Tavily response results must be a list")
    results: list[dict[str, Any]] = []
    for item in raw_results[:max_results]:
        if not isinstance(item, dict):
            continue
        results.append(
            {
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "content": _trim(str(item.get("content") or "")),
                "score": item.get("score"),
            }
        )
    return {
        "query": query,
        "answer": str(raw.get("answer") or ""),
        "results": results,
    }


def _trim(text: str, limit: int = 500) -> str:
    clean = " ".join(text.strip().split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."
