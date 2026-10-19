"""WeChat long-poll inbound delivery."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from telos.adapters.delivery import compact_json
from telos.adapters.wechat.config import WeChatConfig
from telos.adapters.wechat.outbound import PostJsonFn, _default_post_json, _headers

_EP_GET_UPDATES = "ilink/bot/getupdates"
_CHANNEL_VERSION = "2.2.0"
_LONG_POLL_TIMEOUT_MS = 35_000


@dataclass(frozen=True)
class WeChatPollResult:
    ok: bool
    messages: list[dict[str, Any]] = field(default_factory=list)
    sync_buf: str = ""
    suggested_timeout_ms: int = 0
    error: str = ""


GetUpdatesFn = Callable[[str, int], Awaitable[WeChatPollResult] | WeChatPollResult]


def create_wechat_get_updates_fn(
    config: WeChatConfig,
    *,
    post_json_fn: PostJsonFn | None = None,
) -> GetUpdatesFn:
    post_json = post_json_fn or _default_post_json

    async def get_updates(sync_buf: str, timeout_ms: int = _LONG_POLL_TIMEOUT_MS) -> WeChatPollResult:
        if not config.token:
            return WeChatPollResult(ok=False, sync_buf=sync_buf, error="WECHAT_TOKEN is not configured")
        payload = {"get_updates_buf": str(sync_buf or "")}
        body = {**payload, "base_info": {"channel_version": _CHANNEL_VERSION}}
        body_text = compact_json(body)
        url = f"{config.base_url.rstrip('/')}/{_EP_GET_UPDATES}"
        headers = _headers(config.token, body_text)
        try:
            response = post_json(url, body, headers, timeout_ms)
            if hasattr(response, "__await__"):
                response = await response  # type: ignore[assignment]
        except asyncio.TimeoutError:
            return WeChatPollResult(ok=True, sync_buf=sync_buf)
        except Exception as exc:
            return WeChatPollResult(
                ok=False,
                sync_buf=sync_buf,
                error=f"WeChat getupdates transport error: {exc}",
            )

        ret = response.get("ret", 0)
        errcode = response.get("errcode", 0)
        new_sync_buf = str(response.get("get_updates_buf") or sync_buf or "")
        suggested_timeout = _coerce_int(response.get("longpolling_timeout_ms"))
        if ret not in (0, None) or errcode not in (0, None):
            errmsg = response.get("errmsg") or response.get("msg") or "unknown error"
            return WeChatPollResult(
                ok=False,
                sync_buf=new_sync_buf,
                suggested_timeout_ms=suggested_timeout,
                error=f"WeChat getupdates error: ret={ret} errcode={errcode} errmsg={errmsg}",
            )
        messages = [
            message
            for message in list(response.get("msgs") or [])
            if isinstance(message, dict)
        ]
        return WeChatPollResult(
            ok=True,
            messages=messages,
            sync_buf=new_sync_buf,
            suggested_timeout_ms=suggested_timeout,
        )

    return get_updates


def _coerce_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
