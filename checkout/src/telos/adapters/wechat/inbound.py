"""WeChat inbound message normalization."""

from __future__ import annotations

from telos.adapters.base import ConversationRef, InboundTurn
from telos.adapters.wechat.media import ITEM_VOICE, extract_attachments, item_type_value

ITEM_TEXT = 1


def normalize_wechat_event(raw: dict, *, account_id: str = "") -> InboundTurn:
    items = list(raw.get("item_list") or raw.get("items") or [])
    chat_type, chat_id = _guess_chat(raw, account_id)
    sender_id = str(raw.get("sender_id") or raw.get("from_user_id") or sender_fallback(raw) or "").strip()
    text = _extract_text(items)
    ref = ConversationRef(
        platform="wechat",
        tenant_id=account_id,
        chat_id=chat_id,
        user_id=sender_id,
        source_id=f"{chat_type}:{chat_id}",
    )
    return InboundTurn(
        ref=ref,
        text=text,
        attachments=extract_attachments(items),
        raw_event_id=str(raw.get("msg_id") or raw.get("message_id") or raw.get("id") or ""),
        is_group=chat_type == "group",
        mentions_bot=_mentions_bot(raw, text, account_id),
        reply_to=str(raw.get("reply_to") or ""),
        metadata={"has_voice": True, "voice_errors": [
            item["voice_item"]["transcription_error"] for item in items
            if (item.get("voice_item") or {}).get("transcription_error")
        ]} if any(item_type_value(item) == ITEM_VOICE for item in items) else {},
    )


def _guess_chat(raw: dict, account_id: str) -> tuple[str, str]:
    room_id = str(raw.get("room_id") or raw.get("chat_room_id") or "").strip()
    to_user_id = str(raw.get("to_user_id") or "").strip()
    if room_id:
        return "group", room_id
    if to_user_id and account_id and to_user_id != account_id and raw.get("msg_type") == 1:
        return "group", to_user_id
    return "dm", str(raw.get("from_user_id") or sender_fallback(raw) or "").strip()


def sender_fallback(raw: dict) -> str:
    return str(raw.get("sender_id") or raw.get("user_id") or "")


def _extract_text(items: list[dict]) -> str:
    parts = []
    for item in items:
        if item_type_value(item) == ITEM_TEXT:
            text = str((item.get("text_item") or {}).get("text") or item.get("text") or "")
            ref = item.get("ref_msg") or {}
            ref_item = (ref.get("message_item") or {}) if isinstance(ref, dict) else {}
            if ref_item:
                ref_text = _extract_text([ref_item])
                title = str(ref.get("title") or "").strip()
                if ref_text or title:
                    quoted = " | ".join(part for part in [title, ref_text] if part)
                    text = f"[quoted: {quoted}]\n{text}".strip()
            if text.strip():
                parts.append(text)
        elif item_type_value(item) == ITEM_VOICE:
            voice_text = _extract_voice_text(item)
            if voice_text:
                parts.append(_voice_transcript_text(voice_text))
            elif (item.get("voice_item") or {}).get("transcription_error"):
                parts.append("[语音转写失败，本条语音内容未知；请依据后续补充处理，不推测缺失内容。]")
    return "\n\n".join(parts)


def _extract_voice_text(item: dict) -> str:
    voice_item = item.get("voice_item") if isinstance(item.get("voice_item"), dict) else {}
    value = voice_item.get("text")
    if value in (None, ""):
        return ""
    return str(value).strip()


def _voice_transcript_text(text: str) -> str:
    content = str(text or "").strip()
    if not content:
        return ""
    return f"[Voice message received, transcribed below]\n{content}"


def _mentions_bot(raw: dict, text: str, account_id: str) -> bool:
    if bool(raw.get("mentions_bot")):
        return True
    if account_id and account_id in str(raw.get("at_user_list") or ""):
        return True
    return bool(account_id and f"@{account_id}" in text)
