"""Outbound delivery helpers."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from telos.adapters.base import ConversationRef, SendResult

SendTextFn = Callable[[ConversationRef, str], Awaitable[SendResult] | SendResult]

# 仅关联日志；不参与发送、审批或重试。asyncio.to_thread 同样继承该上下文。
delivery_log_context: ContextVar[dict[str, Any] | None] = ContextVar("delivery_log_context", default=None)


def delivery_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16] if value else ""


_BUBBLE_SEPARATOR_RE = re.compile(r"^[ \t]*-{3,}[ \t]*$", re.MULTILINE)


def split_message_bubbles(text: str, max_bubbles: int = 8) -> list[str]:
    content = str(text or "").strip()
    if not content:
        return []
    if "```" in content:
        bubbles = _split_separators_outside_code_fences(content)
        return (bubbles or [content])[: max(1, max_bubbles)]
    bubbles = [
        segment.strip()
        for segment in _BUBBLE_SEPARATOR_RE.split(content)
        if segment.strip()
    ]
    return (bubbles or [content])[: max(1, max_bubbles)]


def _split_separators_outside_code_fences(content: str) -> list[str]:
    bubbles: list[str] = []
    current: list[str] = []
    in_code_fence = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            current.append(line)
            in_code_fence = not in_code_fence
            continue
        if not in_code_fence and _BUBBLE_SEPARATOR_RE.match(line):
            bubble = "\n".join(current).strip()
            if bubble:
                bubbles.append(bubble)
            current = []
            continue
        current.append(line)
    bubble = "\n".join(current).strip()
    if bubble:
        bubbles.append(bubble)
    return bubbles


def split_text_by_length(text: str, max_chars: int) -> list[str]:
    content = str(text or "").strip()
    if not content:
        return []
    if len(content) <= max_chars:
        return [content]
    chunks: list[str] = []
    current = ""
    for block in content.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(block) > max_chars:
            chunks.append(block[:max_chars])
            block = block[max_chars:]
        current = block
    if current:
        chunks.append(current)
    return chunks or [content[:max_chars]]


async def send_text_in_bubbles(
    send_text_fn: SendTextFn,
    ref: ConversationRef,
    text: str,
    *,
    max_bubbles: int,
    max_text_chars: int,
) -> SendResult:
    """Split ``text`` into bubbles/chunks and deliver them one by one.

    Stops at the first failure and reports how many parts made it out.
    """

    parts: list[str] = []
    for bubble in split_message_bubbles(text, max_bubbles=max_bubbles):
        parts.extend(split_text_by_length(bubble, max_text_chars))
    last = SendResult(ok=True)
    sent_count = 0
    for part in parts:
        result = send_text_fn(ref, part)
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[assignment]
        last = result
        if not result.ok:
            return SendResult(
                ok=False,
                message_id=result.message_id,
                error=result.error,
                message_count=sent_count,
            )
        sent_count += 1
    return SendResult(ok=True, message_id=last.message_id, message_count=sent_count)


def compact_json(payload: dict[str, Any]) -> str:
    """Serialize an outbound HTTP body without spaces or ASCII escaping."""

    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
