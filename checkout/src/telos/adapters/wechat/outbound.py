"""WeChat outbound delivery."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import struct
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib import request as urllib_request

from telos.adapters.base import ConversationRef, SendResult
from telos.adapters.delivery import compact_json, send_text_in_bubbles, delivery_fingerprint, delivery_log_context
from telos.adapters.wechat.config import WeChatConfig

SendTextFn = Callable[[ConversationRef, str], Awaitable[SendResult] | SendResult]
ContextTokenProvider = Callable[[ConversationRef], str]
ContextTokenClearer = Callable[[ConversationRef], None]
ClientIdFactory = Callable[[], str]
PostJsonFn = Callable[
    [str, dict[str, Any], dict[str, str], int],
    Awaitable[dict[str, Any]] | dict[str, Any],
]

_ILINK_APP_ID = "bot"
_ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0
_CHANNEL_VERSION = "2.2.0"
_EP_SEND_MESSAGE = "ilink/bot/sendmessage"
_ITEM_TEXT = 1
_MSG_TYPE_BOT = 2
_MSG_STATE_FINISH = 2
_API_TIMEOUT_MS = 15_000
_SESSION_EXPIRED_ERRCODE = -14
_RATE_LIMIT_ERRCODE = -2
logger = logging.getLogger(__name__)


class WeChatOutbound:
    def __init__(self, *, config: WeChatConfig, send_text_fn: SendTextFn):
        self.config = config
        self.send_text_fn = send_text_fn

    async def send_text(self, ref: ConversationRef, text: str, *, protected_text: str = "") -> SendResult:
        return await send_text_in_bubbles(
            self.send_text_fn,
            ref,
            text,
            max_bubbles=self.config.max_bubbles,
            max_text_chars=self.config.max_text_chars,
            split_multiline=self.config.split_multiline_messages,
            protected_text=protected_text,
        )


def create_wechat_send_text_fn(
    config: WeChatConfig,
    *,
    post_json_fn: PostJsonFn | None = None,
    context_token_provider: ContextTokenProvider | None = None,
    context_token_clearer: ContextTokenClearer | None = None,
    client_id_factory: ClientIdFactory | None = None,
) -> SendTextFn:
    post_json = post_json_fn or _default_post_json
    make_client_id = client_id_factory or _default_client_id

    async def send(ref: ConversationRef, text: str) -> SendResult:
        if not config.token:
            return SendResult(ok=False, error="WECHAT_TOKEN is not configured")
        context_token = context_token_provider(ref) if context_token_provider else ""
        retried_without_context = False
        while True:
            client_id = make_client_id()
            payload = _send_message_payload(
                to=ref.chat_id,
                text=text,
                context_token=context_token,
                client_id=client_id,
            )
            body = {**payload, "base_info": {"channel_version": _CHANNEL_VERSION}}
            body_text = compact_json(body)
            url = f"{config.base_url.rstrip('/')}/{_EP_SEND_MESSAGE}"
            headers = _headers(config.token, body_text)
            detail = {**(delivery_log_context.get() or {}), "client_id": client_id,
                      "recipient_fingerprint": delivery_fingerprint(ref.chat_id),
                      "text_fingerprint": delivery_fingerprint(text), "text_chars": len(text),
                      "context_fingerprint": delivery_fingerprint(context_token),
                      "has_context_token": bool(context_token),
                      "attempt": 2 if retried_without_context else 1}
            started = time.monotonic()
            logger.info("微信发送请求", extra={**detail, "event": "wechat.send.attempt"})
            try:
                response = post_json(url, body, headers, _API_TIMEOUT_MS)
                if hasattr(response, "__await__"):
                    response = await response  # type: ignore[assignment]
            except asyncio.CancelledError:
                logger.warning("微信发送等待被取消；请求可能仍在传输", extra={
                    **detail, "event": "wechat.send.cancelled", "receipt": "unknown"})
                raise
            except Exception as exc:
                logger.warning("微信发送传输失败", extra={**detail, "event": "wechat.send.failed",
                    "exception_type": type(exc).__name__, "receipt": "unknown",
                    "elapsed_ms": round((time.monotonic() - started) * 1000)})
                return SendResult(ok=False, error=f"WeChat sendmessage transport error: {exc}")
            ret = response.get("ret", 0)
            errcode = response.get("errcode", 0)
            accepted = ret in (0, None) and errcode in (0, None)
            # 仅记录白名单字段；不保存请求正文、认证头或完整响应。
            # ret 可缺省，不能由 message_id 或 ret 推断客户端已经显示。
            errmsg = str(response.get("errmsg") or response.get("msg") or "")
            for private in (config.token, context_token, ref.chat_id, text):
                if private:
                    errmsg = errmsg.replace(private, "[redacted]")
            logger.log(logging.INFO if accepted else logging.WARNING, "微信发送接口回执", extra={
                **detail, "event": "wechat.send.response", "ret": response.get("ret"),
                "errcode": response.get("errcode"), "errmsg": errmsg[:256],
                "message_id": str(response.get("msg_id") or response.get("message_id") or ""),
                "receipt": "api_accepted" if accepted else "api_rejected",
                "client_delivery": "unknown", "elapsed_ms": round((time.monotonic() - started) * 1000)})
            if ret in (0, None) and errcode in (0, None):
                return SendResult(
                    ok=True,
                    message_id=str(response.get("msg_id") or response.get("message_id") or ""),
                )
            errmsg = response.get("errmsg") or response.get("msg") or "unknown error"
            if (
                context_token
                and not retried_without_context
                and _is_stale_session(ret, errcode, errmsg)
            ):
                retried_without_context = True
                logger.warning("会话令牌失效，沿用现有策略移除令牌重试", extra={
                    **detail, "event": "wechat.send.context_retry"})
                context_token = ""
                if context_token_clearer is not None:
                    context_token_clearer(ref)
                continue
            return SendResult(
                ok=False,
                error=f"WeChat sendmessage error: ret={ret} errcode={errcode} errmsg={errmsg}",
            )

    return send


def _send_message_payload(
    *,
    to: str,
    text: str,
    context_token: str,
    client_id: str,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "from_user_id": "",
        "to_user_id": to,
        "client_id": client_id,
        "message_type": _MSG_TYPE_BOT,
        "message_state": _MSG_STATE_FINISH,
        "item_list": [{"type": _ITEM_TEXT, "text_item": {"text": text}}],
    }
    if context_token:
        message["context_token"] = context_token
    return {"msg": message}


def _headers(token: str, body_text: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body_text.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": _ILINK_APP_ID,
        "iLink-App-ClientVersion": str(_ILINK_APP_CLIENT_VERSION),
        "Authorization": f"Bearer {token}",
    }


def _is_stale_session(ret: object, errcode: object, errmsg: object) -> bool:
    if ret == _SESSION_EXPIRED_ERRCODE or errcode == _SESSION_EXPIRED_ERRCODE:
        return True
    if ret != _RATE_LIMIT_ERRCODE and errcode != _RATE_LIMIT_ERRCODE:
        return False
    return str(errmsg or "").strip().lower() == "unknown error"


def _default_client_id() -> str:
    return f"telos-wechat-{secrets.token_hex(16)}"


def _random_wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


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
    # getupdates 也复用 HTTP 传输；此处只审计 sendmessage，避免改变轮询。
    message = payload.get("msg")
    detail = ({**(delivery_log_context.get() or {}), "client_id": message.get("client_id", "")}
              if isinstance(message, dict) else None)
    try:
        with urllib_request.urlopen(req, timeout=timeout_ms / 1000) as response:
            raw = response.read().decode("utf-8")
            if detail is not None:
                logger.info("微信发送 HTTP 回执", extra={**detail, "event": "wechat.send.http",
                    "http_status": response.status, "response_bytes": len(raw.encode("utf-8"))})
    except Exception as exc:
        if detail is not None:
            logger.warning("微信发送 HTTP 异常", extra={**detail, "event": "wechat.send.http_failed",
                "http_status": getattr(exc, "code", None), "exception_type": type(exc).__name__})
        raise
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("WeChat sendmessage response must be a JSON object")
    return parsed
