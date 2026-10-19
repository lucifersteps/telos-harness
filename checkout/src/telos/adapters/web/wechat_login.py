"""进程内单会话的微信扫码登录状态（本机单用户，无需并发隔离）。

onboarding 向导和设置页的重新登录共用这一份状态机。
"""

from __future__ import annotations

import asyncio
from typing import Any


class WeChatLoginSession:
    def __init__(self) -> None:
        self.task: asyncio.Task[Any] | None = None
        self.phase = "idle"  # idle | starting | waiting | confirmed | error
        self.qr_image = ""
        self.account: dict[str, Any] | None = None
        self.error = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "qr_image": self.qr_image,
            "account": self.account,
            "error": self.error,
        }
