"""Adapter-specific approval channel helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from telos.adapters.base import Adapter, ApprovalRequest, ApprovalResponse, ConversationRef


class ApprovalChannel(Protocol):
    async def request_approval(self, request: ApprovalRequest) -> ApprovalResponse: ...


class SharedApprovalRegistry(Protocol):
    """进程级、连接无关的待审批登记处（按 ``request_id`` 寻址）。

    web 的 ``PendingApprovalRegistry`` 结构化满足它。让消息端适配器也能把审批 Future
    登记进同一处——于是同一条消息端审批既可在文字回复作答，也可在任一 web 端作答，
    先到者兑现。声明在这个中立模块，避免消息端适配器反向依赖 web 层。
    """

    def register(self, request: ApprovalRequest) -> "asyncio.Future[ApprovalResponse]": ...
    def resolve(
        self,
        request_id: str,
        decision: str,
        feedback: str = "",
        *,
        scope: str = "",
        modified_args: dict | None = None,
    ) -> bool: ...
    def cancel_by_turn(
        self,
        turn_id: str,
        feedback: str = "cancelled by stop",
    ) -> int: ...
    def cancel_by_tool_call(
        self,
        tool_call_id: str,
        feedback: str = "cancelled by stop",
    ) -> int: ...
    def discard(self, request_id: str) -> None: ...


@dataclass(frozen=True)
class AdapterApprovalChannel:
    adapter: Adapter
    ref: ConversationRef

    async def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        return await self.adapter.request_approval(self.ref, request)
