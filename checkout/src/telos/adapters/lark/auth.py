"""Lark webhook authentication helpers."""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any

from telos.adapters.lark.config import LarkConfig


@dataclass(frozen=True)
class LarkWebhookDecision:
    allowed: bool
    reason: str = ""


def authorize_lark_event(raw: dict[str, Any], config: LarkConfig) -> LarkWebhookDecision:
    if _is_encrypted_callback(raw):
        return LarkWebhookDecision(False, "encrypted lark callbacks are not supported yet")
    expected = str(config.verification_token or "").strip()
    if not expected:
        return LarkWebhookDecision(True)
    token = _event_token(raw)
    if not token:
        return LarkWebhookDecision(False, "missing lark verification token")
    if hmac.compare_digest(token, expected):
        return LarkWebhookDecision(True)
    return LarkWebhookDecision(False, "invalid lark verification token")


def lark_url_verification_response(
    raw: dict[str, Any],
    config: LarkConfig,
) -> dict[str, str] | None:
    if not is_lark_url_verification(raw):
        return None
    decision = authorize_lark_event(raw, config)
    if not decision.allowed:
        raise ValueError(decision.reason)
    challenge = _challenge(raw)
    if not challenge:
        raise ValueError("missing lark challenge")
    return {"challenge": challenge}


def is_lark_url_verification(raw: dict[str, Any]) -> bool:
    if str(raw.get("type") or "") == "url_verification":
        return True
    header = raw.get("header") if isinstance(raw.get("header"), dict) else {}
    if str(header.get("event_type") or "") == "url_verification":
        return True
    event = raw.get("event") if isinstance(raw.get("event"), dict) else {}
    return str(event.get("type") or "") == "url_verification"


def _is_encrypted_callback(raw: dict[str, Any]) -> bool:
    return bool(raw.get("encrypt"))


def _event_token(raw: dict[str, Any]) -> str:
    header = raw.get("header") if isinstance(raw.get("header"), dict) else {}
    event = raw.get("event") if isinstance(raw.get("event"), dict) else {}
    return str(
        header.get("token")
        or raw.get("token")
        or event.get("token")
        or ""
    ).strip()


def _challenge(raw: dict[str, Any]) -> str:
    event = raw.get("event") if isinstance(raw.get("event"), dict) else {}
    return str(raw.get("challenge") or event.get("challenge") or "").strip()
