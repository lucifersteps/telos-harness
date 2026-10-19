"""Lark inbound event normalization."""

from __future__ import annotations

import json

from telos.adapters.base import ConversationRef, InboundTurn
from telos.adapters.lark.media import extract_lark_attachments


def normalize_lark_event(raw: dict) -> InboundTurn:
    event = raw.get("event") if isinstance(raw.get("event"), dict) else raw
    message = event.get("message") if isinstance(event.get("message"), dict) else event
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    chat_id = str(message.get("chat_id") or event.get("chat_id") or "")
    thread_id = str(message.get("thread_id") or message.get("root_id") or "")
    user_id = _sender_id(sender)
    chat_type = str(message.get("chat_type") or event.get("chat_type") or "p2p")
    text = _extract_lark_text(message)
    ref = ConversationRef(
        platform="lark",
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=user_id,
        source_id=f"{chat_type}:{chat_id}:{thread_id}",
    )
    return InboundTurn(
        ref=ref,
        text=text,
        attachments=extract_lark_attachments(message),
        raw_event_id=str(message.get("message_id") or event.get("event_id") or ""),
        is_group=chat_type != "p2p",
        mentions_bot=bool(message.get("mentions") or event.get("mentions_bot")),
    )


def _sender_id(sender: dict) -> str:
    sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else sender
    if isinstance(sender_id, dict):
        return str(
            sender_id.get("open_id")
            or sender_id.get("user_id")
            or sender_id.get("union_id")
            or ""
        )
    return str(sender.get("open_id") or sender.get("user_id") or "")


def _extract_lark_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return content
        if isinstance(parsed, dict):
            return str(parsed.get("text") or parsed.get("content") or "")
        return content
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    return str(message.get("text") or "")
