"""WeChat iLink QR login helpers."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib import request as urllib_request
from urllib.parse import quote

import segno

from telos.adapters.wechat.config import WeChatConfig
from telos.adapters.wechat.state import WeChatAccount, WeChatStateStore


GetJsonFn = Callable[
    [str, dict[str, str], int],
    Awaitable[dict[str, Any]] | dict[str, Any],
]
SleepFn = Callable[[float], Awaitable[None]]
ClockFn = Callable[[], float]

_EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
_EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"
_ILINK_APP_ID = "bot"
_ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0
_QR_TIMEOUT_MS = 35_000


@dataclass(frozen=True)
class WeChatQrChallenge:
    qrcode: str
    image_url: str = ""
    scan_data: str = ""
    base_url: str = ""


ChallengeCallback = Callable[[WeChatQrChallenge], None]


@dataclass(frozen=True)
class WeChatQrLoginResult:
    ok: bool
    account: WeChatAccount | None = None
    challenge: WeChatQrChallenge | None = None
    status: str = ""
    error: str = ""


async def request_wechat_qr_challenge(
    config: WeChatConfig,
    *,
    bot_type: str = "3",
    get_json_fn: GetJsonFn | None = None,
) -> WeChatQrChallenge:
    get_json = get_json_fn or _default_get_json
    url = f"{config.base_url.rstrip('/')}/{_EP_GET_BOT_QR}?bot_type={quote(str(bot_type))}"
    response = get_json(url, _headers(), _QR_TIMEOUT_MS)
    if hasattr(response, "__await__"):
        response = await response  # type: ignore[assignment]
    qrcode = str(response.get("qrcode") or "").strip()
    # `qrcode_img_content` 名字像图片，实际是要被"编码进二维码"的落地页 URL
    # （如 https://liteapp.weixin.qq.com/q/...），不是可直接 <img src> 的图。
    # 这里把它渲染成真正的二维码 PNG（data URI）供前端展示；scan_data 仍存原始内容。
    scan_content = str(response.get("qrcode_img_content") or "").strip() or qrcode
    if not qrcode:
        raise ValueError("WeChat QR response missing qrcode")
    return WeChatQrChallenge(
        qrcode=qrcode,
        image_url=_qr_png_data_uri(scan_content),
        scan_data=scan_content,
        base_url=config.base_url,
    )


def _qr_png_data_uri(content: str) -> str:
    """把二维码内容渲染成可直接 <img src> 的 PNG data URI。"""

    buffer = io.BytesIO()
    segno.make(content, error="m").save(buffer, kind="png", scale=6, border=2)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


async def run_wechat_qr_login(
    config: WeChatConfig,
    *,
    state_store: WeChatStateStore | None = None,
    bot_type: str = "3",
    timeout_seconds: int = 480,
    max_refreshes: int = 3,
    get_json_fn: GetJsonFn | None = None,
    sleep_fn: SleepFn | None = None,
    clock_fn: ClockFn | None = None,
    challenge_callback: ChallengeCallback | None = None,
) -> WeChatQrLoginResult:
    get_json = get_json_fn or _default_get_json
    sleep = sleep_fn or asyncio.sleep
    now = clock_fn or time.time
    try:
        challenge = await request_wechat_qr_challenge(
            config,
            bot_type=bot_type,
            get_json_fn=get_json,
        )
    except Exception as exc:
        return WeChatQrLoginResult(ok=False, error=f"WeChat QR request error: {exc}")
    if challenge_callback is not None:
        challenge_callback(challenge)

    deadline = now() + max(0, int(timeout_seconds))
    current_base_url = config.base_url
    refreshes = 0
    while now() < deadline:
        try:
            response = get_json(
                f"{current_base_url.rstrip('/')}/{_EP_GET_QR_STATUS}?qrcode={quote(challenge.qrcode)}",
                _headers(),
                _QR_TIMEOUT_MS,
            )
            if hasattr(response, "__await__"):
                response = await response  # type: ignore[assignment]
        except asyncio.TimeoutError:
            await sleep(1)
            continue
        except Exception as exc:
            return WeChatQrLoginResult(
                ok=False,
                challenge=challenge,
                error=f"WeChat QR status transport error: {exc}",
            )

        status = str(response.get("status") or "wait")
        if status in {"wait", "scaned"}:
            await sleep(1)
            continue
        if status == "scaned_but_redirect":
            redirect_host = str(response.get("redirect_host") or "").strip()
            if redirect_host:
                current_base_url = f"https://{redirect_host}"
            await sleep(1)
            continue
        if status == "expired":
            refreshes += 1
            if refreshes > max_refreshes:
                return WeChatQrLoginResult(
                    ok=False,
                    challenge=challenge,
                    status=status,
                    error="WeChat QR code expired too many times",
                )
            try:
                challenge = await request_wechat_qr_challenge(
                    config,
                    bot_type=bot_type,
                    get_json_fn=get_json,
                )
            except Exception as exc:
                return WeChatQrLoginResult(
                    ok=False,
                    status=status,
                    error=f"WeChat QR refresh error: {exc}",
                )
            if challenge_callback is not None:
                challenge_callback(challenge)
            current_base_url = config.base_url
            await sleep(1)
            continue
        if status == "confirmed":
            account = _account_from_confirmed(response, config)
            if account is None:
                return WeChatQrLoginResult(
                    ok=False,
                    challenge=challenge,
                    status=status,
                    error="WeChat QR confirmed but credential payload is incomplete",
                )
            if state_store is not None:
                state_store.save_account(account)
            return WeChatQrLoginResult(
                ok=True,
                account=account,
                challenge=challenge,
                status=status,
            )
        return WeChatQrLoginResult(
            ok=False,
            challenge=challenge,
            status=status,
            error=f"WeChat QR status is not supported: {status}",
        )

    return WeChatQrLoginResult(
        ok=False,
        challenge=challenge,
        status="timeout",
        error="WeChat QR login timed out",
    )


def _account_from_confirmed(response: dict[str, Any], config: WeChatConfig) -> WeChatAccount | None:
    account_id = str(response.get("ilink_bot_id") or "").strip()
    token = str(response.get("bot_token") or "").strip()
    if not account_id or not token:
        return None
    return WeChatAccount(
        account_id=account_id,
        token=token,
        base_url=str(response.get("baseurl") or config.base_url),
        user_id=str(response.get("ilink_user_id") or ""),
    )


def _headers() -> dict[str, str]:
    return {
        "iLink-App-Id": _ILINK_APP_ID,
        "iLink-App-ClientVersion": str(_ILINK_APP_CLIENT_VERSION),
    }


async def _default_get_json(
    url: str,
    headers: dict[str, str],
    timeout_ms: int,
) -> dict[str, Any]:
    return await asyncio.to_thread(_get_json_blocking, url, headers, timeout_ms)


def _get_json_blocking(
    url: str,
    headers: dict[str, str],
    timeout_ms: int,
) -> dict[str, Any]:
    req = urllib_request.Request(url, headers=headers, method="GET")
    with urllib_request.urlopen(req, timeout=timeout_ms / 1000) as response:
        raw = response.read().decode("utf-8")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("WeChat QR login response must be a JSON object")
    return parsed
