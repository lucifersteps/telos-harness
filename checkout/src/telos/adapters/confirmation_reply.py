"""Classify user replies to pending command confirmations."""

from __future__ import annotations

from dataclasses import dataclass, replace

from telos.adapters.base import InboundTurn

_CONFIRM_TOKENS = frozenset(
    {"1", "y", "yes", "ok", "confirm", "confirmed", "同意", "确认", "好", "可以"}
)
_CANCEL_TOKENS = frozenset(
    {"0", "n", "no", "cancel", "cancelled", "取消", "拒绝", "不", "不要"}
)


@dataclass(frozen=True)
class ConfirmationReply:
    confirmed: bool
    command_text: str = ""


def classify_confirmation_reply(text: str) -> ConfirmationReply | None:
    raw = str(text or "").strip()
    token = raw.lower()
    if token in _CONFIRM_TOKENS:
        return ConfirmationReply(confirmed=True)
    if token in _CANCEL_TOKENS:
        return ConfirmationReply(confirmed=False)
    head, _, tail = raw.partition(" ")
    command_text = tail.strip()
    if command_text.startswith("/"):
        normalized_head = head.strip().lower()
        if normalized_head in _CONFIRM_TOKENS:
            return ConfirmationReply(confirmed=True, command_text=command_text)
        if normalized_head in _CANCEL_TOKENS:
            return ConfirmationReply(confirmed=False, command_text=command_text)
    return None


def confirmation_response_turn(
    turn: InboundTurn,
    *,
    request_id: str,
    decision: str,
    command_text: str = "",
    feedback: str = "",
) -> InboundTurn:
    metadata = dict(turn.metadata or {})
    metadata.update(
        {
            "input_kind": "confirmation_response",
            "input_request_id": str(request_id or ""),
            "decision": str(decision or ""),
            "command_text": str(command_text or ""),
            "feedback": str(feedback or ""),
        }
    )
    return replace(turn, metadata=metadata)
