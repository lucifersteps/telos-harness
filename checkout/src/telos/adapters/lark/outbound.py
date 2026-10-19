"""Lark outbound delivery."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib import request as urllib_request

from telos.adapters.base import ConversationRef, SendResult
from telos.adapters.delivery import compact_json, send_text_in_bubbles
from telos.adapters.lark.config import LarkConfig

SendTextFn = Callable[[ConversationRef, str], Awaitable[SendResult] | SendResult]
PostJsonFn = Callable[
    [str, dict[str, Any], dict[str, str], int],
    Awaitable[dict[str, Any]] | dict[str, Any],
]
ClockFn = Callable[[], float]

_EP_TENANT_TOKEN = "open-apis/auth/v3/tenant_access_token/internal"
_EP_MESSAGES = "open-apis/im/v1/messages"
_API_TIMEOUT_MS = 15_000
_TOKEN_EXPIRY_MARGIN_SECONDS = 60


class LarkOutbound:
    def __init__(self, *, config: LarkConfig, send_text_fn: SendTextFn):
        self.config = config
        self.send_text_fn = send_text_fn

    async def send_text(self, ref: ConversationRef, text: str) -> SendResult:
        return await send_text_in_bubbles(
            self.send_text_fn,
            ref,
            text,
            max_bubbles=self.config.max_bubbles,
            max_text_chars=self.config.max_text_chars,
        )


def create_lark_send_text_fn(
    config: LarkConfig,
    *,
    post_json_fn: PostJsonFn | None = None,
    clock_fn: ClockFn | None = None,
) -> SendTextFn:
    post_json = post_json_fn or _default_post_json
    now = clock_fn or time.time
    cached_token = ""
    token_expires_at = 0.0

    async def send(ref: ConversationRef, text: str) -> SendResult:
        nonlocal cached_token, token_expires_at
        token_result = await _tenant_token(
            config,
            post_json=post_json,
            now=now,
            cached_token=cached_token,
            token_expires_at=token_expires_at,
        )
        if token_result.error:
            return SendResult(ok=False, error=token_result.error)

        cached_token = token_result.token
        token_expires_at = token_result.expires_at

        payload = {
            "receive_id": ref.chat_id,
            "msg_type": "text",
            "content": compact_json({"text": text}),
        }
        url = f"{config.base_url.rstrip('/')}/{_EP_MESSAGES}?receive_id_type=chat_id"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cached_token}",
        }
        try:
            response = post_json(url, payload, headers, _API_TIMEOUT_MS)
            if hasattr(response, "__await__"):
                response = await response  # type: ignore[assignment]
        except Exception as exc:
            return SendResult(ok=False, error=f"Lark send message transport error: {exc}")

        code = response.get("code", 0)
        if code in (0, None):
            data = response.get("data") if isinstance(response.get("data"), dict) else {}
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            return SendResult(
                ok=True,
                message_id=str(data.get("message_id") or message.get("message_id") or ""),
            )
        message = response.get("msg") or response.get("message") or "unknown error"
        return SendResult(ok=False, error=f"Lark send message error: code={code} msg={message}")

    return send


class _TokenResult:
    def __init__(self, *, token: str = "", expires_at: float = 0.0, error: str = ""):
        self.token = token
        self.expires_at = expires_at
        self.error = error


async def _tenant_token(
    config: LarkConfig,
    *,
    post_json: PostJsonFn,
    now: ClockFn,
    cached_token: str,
    token_expires_at: float,
) -> _TokenResult:
    if cached_token and now() < token_expires_at:
        return _TokenResult(token=cached_token, expires_at=token_expires_at)
    if not config.app_id or not config.app_secret:
        return _TokenResult(error="LARK_APP_ID/LARK_APP_SECRET is not configured")

    payload = {"app_id": config.app_id, "app_secret": config.app_secret}
    url = f"{config.base_url.rstrip('/')}/{_EP_TENANT_TOKEN}"
    headers = {"Content-Type": "application/json"}
    try:
        response = post_json(url, payload, headers, _API_TIMEOUT_MS)
        if hasattr(response, "__await__"):
            response = await response  # type: ignore[assignment]
    except Exception as exc:
        return _TokenResult(error=f"Lark tenant_access_token transport error: {exc}")

    code = response.get("code", 0)
    token = str(response.get("tenant_access_token") or "")
    if code not in (0, None) or not token:
        message = response.get("msg") or response.get("message") or "missing tenant_access_token"
        return _TokenResult(
            error=f"Lark tenant_access_token error: code={code} msg={message}",
        )

    expire_seconds = _coerce_expire_seconds(response.get("expire"))
    expires_at = now() + max(0, expire_seconds - _TOKEN_EXPIRY_MARGIN_SECONDS)
    return _TokenResult(token=token, expires_at=expires_at)


def _coerce_expire_seconds(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


async def _default_post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout_ms: int,
) -> dict[str, Any]:
    return await asyncio.to_thread(_post_json_blocking, url, payload, headers, timeout_ms)


def _post_json_blocking(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout_ms: int,
) -> dict[str, Any]:
    data = compact_json(payload).encode("utf-8")
    req = urllib_request.Request(url, data=data, headers=headers, method="POST")
    with urllib_request.urlopen(req, timeout=timeout_ms / 1000) as response:
        raw = response.read().decode("utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("Lark response must be a JSON object")
    return parsed
