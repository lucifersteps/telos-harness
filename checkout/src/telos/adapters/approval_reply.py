"""Classify whether an inbound message is an approval verdict.

Shared across adapters so pending-approval routing keys on *content*, not just
conversation identity. A message that is not a recognizable approve/deny token is
NOT an approval verdict. Adapters may route it as recorded feedback that rejects
the pending preview and returns the text to the agent, or as a normal new message;
it must never be silently discarded or treated as approval.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from telos.adapters.base import InboundTurn

_APPROVE_TOKENS = frozenset(
    {"1", "y", "yes", "ok", "approve", "approved", "同意", "确认", "好", "可以"}
)
_CANCEL_TOKENS = frozenset({"2", "cancel", "cancelled", "canceled", "取消"})
_DENY_TOKENS = frozenset(
    {"0", "n", "no", "deny", "denied", "reject", "rejected", "拒绝", "不", "不要"}
)


@dataclass(frozen=True)
class ApprovalReply:
    approved: bool
    target_id: str = ""
    decision: str = ""


def classify_approval_reply(text: str) -> ApprovalReply | None:
    """Return an :class:`ApprovalReply` for an explicit verdict token, else None."""
    raw = str(text or "").strip()
    token = raw.lower()
    if token in _APPROVE_TOKENS:
        return ApprovalReply(approved=True, decision="approve")
    if token in _CANCEL_TOKENS:
        return ApprovalReply(approved=False, decision="cancelled")
    if token in _DENY_TOKENS:
        return ApprovalReply(approved=False, decision="deny")
    head, _, tail = raw.partition(" ")
    target_id = tail.strip()
    if _looks_like_approval_target(target_id):
        normalized_head = head.strip().lower()
        if normalized_head in _APPROVE_TOKENS:
            return ApprovalReply(approved=True, target_id=target_id, decision="approve")
        if normalized_head in _CANCEL_TOKENS:
            return ApprovalReply(
                approved=False,
                target_id=target_id,
                decision="cancelled",
            )
        if normalized_head in _DENY_TOKENS:
            return ApprovalReply(approved=False, target_id=target_id, decision="deny")
    return None


def _looks_like_approval_target(value: str) -> bool:
    target = str(value or "").strip()
    return target.startswith(("call_", "appr_"))


def approval_response_turn(
    turn: InboundTurn,
    *,
    request_id: str = "",
    pending_request_ids: list[str] | None = None,
    tool_call_id: str = "",
    decision: str,
    feedback: str = "",
    scope: str = "",
    modified_args: dict | None = None,
    runtime_missing: bool = False,
    runtime_resolved: bool = False,
    runtime_cancelled: bool = False,
    stop_cascade: bool = False,
) -> InboundTurn:
    metadata = dict(turn.metadata or {})
    pending_ids = [str(item) for item in list(pending_request_ids or []) if str(item)]
    metadata.update(
        {
            "input_kind": "approval_response",
            "approval_request_id": str(request_id or ""),
            "pending_request_ids": pending_ids,
            "tool_call_id": str(tool_call_id or ""),
            "decision": str(decision or ""),
            "feedback": str(feedback or ""),
            "scope": str(scope or ""),
            "has_modified_args": modified_args is not None,
            "approval_runtime_missing": bool(runtime_missing),
            "approval_runtime_resolved": bool(runtime_resolved),
            "approval_runtime_cancelled": bool(runtime_cancelled),
            "stop_cascade": bool(stop_cascade),
        }
    )
    if modified_args is not None:
        metadata["modified_args"] = dict(modified_args)
    return replace(turn, metadata=metadata)
