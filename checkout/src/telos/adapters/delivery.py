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
_LIST_ITEM_RE = re.compile(r"^[ \t]*(?:[-+*]|\d+[.)、])[ \t]+\S")
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})")


def _is_list_heading(line: str) -> bool:
    text = line.strip()
    return text.endswith((":", "：")) or text.startswith("#") or (
        text.startswith("**") and text.endswith("**")
    )


def _split_chat_bubbles(content: str) -> list[str]:
    lines = content.splitlines()
    bubbles: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip() or _BUBBLE_SEPARATOR_RE.fullmatch(line):
            index += 1
            continue
        start = index
        # Keep a list's heading with its items, including the usual blank line.
        if _is_list_heading(line):
            following = index + 1
            while following < len(lines) and not lines[following].strip():
                following += 1
            if following < len(lines) and _LIST_ITEM_RE.match(lines[following]):
                index = following
                line = lines[index]
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence[1]
            index += 1
            while index < len(lines):
                closing = lines[index].strip()
                index += 1
                if closing and set(closing) == {marker[0]} and len(closing) >= len(marker):
                    break
        elif _LIST_ITEM_RE.match(line):
            index += 1
            while index < len(lines):
                continuation = lines[index]
                if _BUBBLE_SEPARATOR_RE.fullmatch(continuation):
                    break
                if not continuation.strip():
                    following = index + 1
                    while following < len(lines) and not lines[following].strip():
                        following += 1
                    if following == len(lines):
                        break
                    continuation = lines[following]
                    if not (_LIST_ITEM_RE.match(continuation) or continuation.startswith((" ", "\t"))):
                        break
                elif not (_LIST_ITEM_RE.match(continuation) or continuation.startswith((" ", "\t"))):
                    break
                index += 1
        else:
            index += 1
        bubbles.append("\n".join(lines[start:index]).strip())
    return bubbles


def split_message_bubbles(text: str, max_bubbles: int | None = 8) -> list[str]:
    content = str(text or "").strip()
    if not content:
        return []
    if "```" in content:
        bubbles = _split_separators_outside_code_fences(content)
        return (bubbles or [content]) if max_bubbles is None else (bubbles or [content])[: max(1, max_bubbles)]
    bubbles = [
        segment.strip()
        for segment in _BUBBLE_SEPARATOR_RE.split(content)
        if segment.strip()
    ]
    return (bubbles or [content]) if max_bubbles is None else (bubbles or [content])[: max(1, max_bubbles)]


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
    max_bubbles: int | None,
    max_text_chars: int,
    split_multiline: bool = False,
    protected_text: str = "",
) -> SendResult:
    """Split ``text`` into bubbles/chunks and deliver them one by one.

    Stops at the first failure and reports how many parts made it out.
    """

    parts: list[str] = []
    bubbles = (split_reply_bubbles(text, protected_text=protected_text)
               if split_multiline else split_message_bubbles(text, max_bubbles=max_bubbles))
    for bubble in bubbles:
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


def split_reply_bubbles(text: str, *, protected_text: str = "") -> list[str]:
    """One thought per bubble, preserving lists, code and authoritative previews.

    This is only presentation: no extra model turns, markers or persisted state.
    Plain chat lines split individually; structured blocks retain their newlines.
    All content is delivered without a bubble count limit.
    """
    if protected_text and protected_text in text:
        before, _, after = text.partition(protected_text)
        return [*_split_chat_bubbles(before), protected_text, *_split_chat_bubbles(after)]
    return _split_chat_bubbles(text)


def compact_json(payload: dict[str, Any]) -> str:
    """Serialize an outbound HTTP body without spaces or ASCII escaping."""

    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
