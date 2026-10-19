"""按会话串行化助手 turn。

一个助手会话可能同时被两个入口驱动（用户直连 + 主 agent 工具委派）。当前实现用
按会话 id 分键的进程内 ``asyncio.Lock`` 串行化 —— 仅覆盖同进程，与单 daemon 部署
形态一致；只有 daemon 变成多进程时才需要文件锁。
"""

from __future__ import annotations

import asyncio


class AssistantLocks:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock
