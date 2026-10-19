"""WeChat inbound authorization helpers."""

from __future__ import annotations

from dataclasses import dataclass

from telos.adapters.base import InboundTurn
from telos.adapters.wechat.config import WeChatConfig


@dataclass(frozen=True)
class WeChatInboundDecision:
    allowed: bool
    reason: str = ""


def authorize_wechat_turn(turn: InboundTurn, config: WeChatConfig) -> WeChatInboundDecision:
    sender_id = str(turn.ref.user_id or "").strip()
    if not sender_id:
        return WeChatInboundDecision(False, "missing sender")
    if config.account_id and sender_id == config.account_id:
        return WeChatInboundDecision(False, "self message")
    if turn.is_group:
        return _authorize_group_turn(turn, config)
    return _authorize_dm_turn(turn, config)


def _authorize_dm_turn(turn: InboundTurn, config: WeChatConfig) -> WeChatInboundDecision:
    policy = _normalize_policy(config.dm_policy)
    if policy == "disabled":
        return WeChatInboundDecision(False, "dm disabled")
    if policy == "allowlist":
        sender_id = str(turn.ref.user_id or "").strip()
        if sender_id in set(config.allowed_users):
            return WeChatInboundDecision(True)
        return WeChatInboundDecision(False, "dm sender not allowlisted")
    return WeChatInboundDecision(True)


def _authorize_group_turn(turn: InboundTurn, config: WeChatConfig) -> WeChatInboundDecision:
    policy = _normalize_policy(config.group_policy)
    if policy == "disabled":
        return WeChatInboundDecision(False, "group disabled")
    if policy == "allowlist":
        allowed_chats = set(config.group_allowed_users)
        if turn.ref.chat_id in allowed_chats:
            return WeChatInboundDecision(True)
        return WeChatInboundDecision(False, "group chat not allowlisted")
    if policy in {"mentions", "mention"} and not turn.mentions_bot:
        return WeChatInboundDecision(False, "group mention required")
    return WeChatInboundDecision(True)


def _normalize_policy(value: str) -> str:
    policy = str(value or "").strip().lower()
    if policy in {"off", "deny", "closed", "false", "0"}:
        return "disabled"
    if policy in {"allow_list", "allowed", "restricted"}:
        return "allowlist"
    if not policy:
        return "open"
    return policy
