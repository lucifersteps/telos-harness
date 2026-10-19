"""Shared helpers for memory-space run traces."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from telos.providers.base import Message
from telos.storage.filesystem import ensure_dir
from telos.storage.jsonl import append_jsonl

RUN_TRACE_VERSION = 1
_POSIX_ABSOLUTE_PATH_RE = re.compile(r"(?<![:/\w])/(?:[^\s\"'<>`]+)")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"\b[A-Za-z]:\\(?:[^\s\"'<>`]+)")


def attempt_count_from_retry_events(retry_events: list[dict[str, Any]]) -> int:
    return len(list(retry_events or [])) + 1


def sanitize_run_error(error: object) -> str:
    text = str(error or "")
    if not text:
        return ""
    text = _POSIX_ABSOLUTE_PATH_RE.sub("<local-path>", text)
    return _WINDOWS_ABSOLUTE_PATH_RE.sub("<local-path>", text)


def sanitize_run_retry_events(retry_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for event in list(retry_events or []):
        data = dict(event)
        for key, value in list(data.items()):
            if isinstance(value, str):
                data[key] = sanitize_run_error(value)
        sanitized.append(data)
    return sanitized


def run_context_message_item(
    message: Message,
    *,
    source: str,
    content: object | None = None,
) -> dict[str, Any]:
    """Serialize a provider-visible message for a run-local context trace."""
    data: dict[str, Any] = {
        "type": "message",
        "source": source,
        "role": message.role,
        "content": message.content if content is None else content,
        "tool_calls": [dict(item) for item in list(message.tool_calls or [])],
        "tool_call_id": message.tool_call_id,
        "tool_name": message.name,
    }
    if isinstance(message.cache_control, dict):
        data["cache_control"] = dict(message.cache_control)
    return data


def start_run_context_file(
    run_dir: Path,
    *,
    system_prompt: str,
    system_prompt_source: str,
) -> Path:
    """Truncate ``run_dir/context.jsonl`` and seed it with the system prompt.

    Shared by the recap and timeline-summary run traces so both start from the
    same file layout.
    """

    ensure_dir(run_dir)
    context_path = run_dir / "context.jsonl"
    if context_path.exists():
        context_path.unlink()
    text = str(system_prompt or "").strip()
    if text:
        append_jsonl(
            context_path,
            run_context_message_item(
                Message(role="system", content=text),
                source=system_prompt_source,
            ),
        )
    return context_path
