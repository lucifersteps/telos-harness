"""WebAdapter：实现 `Adapter` 协议，供主对话的 `TurnCoordinator` 使用。

流式与最终回复都由 WS + RuntimeView attach 处理，所以 `send_text` 是 no-op；
`request_approval` 委托给连接无关的 `RegistryApprovalChannel`（审批请求经 live stream
的 `tool.call.approval_request` 事件送达前端，Future 存在进程级 registry 里，按
`request_id` 作答），因此不再需要按连接绑定审批通道。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from telos.adapters.base import (
    AdapterCapabilities,
    ApprovalRequest,
    ApprovalResponse,
    ConversationRef,
    InboundTurn,
    SendResult,
)
from telos.adapters.question_channel import QuestionRequest, QuestionResponse
from telos.adapters.web.approval_registry import (
    PendingApprovalRegistry,
    RegistryApprovalChannel,
)
from telos.adapters.question_registry import RegistryQuestionChannel


class WebAdapter:
    name = "web"
    capabilities = AdapterCapabilities(
        max_text_chars=100_000,
        supports_buttons=True,
        supports_threads=False,
        supports_files=False,
    )

    def __init__(
        self,
        approval_channel: RegistryApprovalChannel | None = None,
        question_channel: RegistryQuestionChannel | None = None,
    ) -> None:
        # 无显式通道时（如 daemon 投递适配器登记）自带一个隔离 registry：没人应答就超时，
        # 不会误拒；交互式 web 控制台在 apps/web.py 传入共享通道。
        self._approval_channel = approval_channel or RegistryApprovalChannel(
            PendingApprovalRegistry()
        )
        self._question_channel = question_channel

    async def receive(self) -> AsyncIterator[InboundTurn]:
        # web 是请求驱动的（WS handler 直接调 coordinator），不走轮询 receive。
        if False:  # pragma: no cover
            yield InboundTurn(ref=ConversationRef(platform="web"), text="")
        return

    def drain_recorded_inputs(self) -> list[InboundTurn]:
        return []

    async def send_text(self, ref: ConversationRef, text: str) -> SendResult:
        # 最终文本已经通过流式 + turn.completed 送达前端，这里不重复投递。
        del ref, text
        return SendResult(ok=True)

    async def request_approval(
        self,
        ref: ConversationRef,
        request: ApprovalRequest,
    ) -> ApprovalResponse:
        del ref
        return await self._approval_channel.request_approval(request)

    async def request_question(
        self,
        ref: ConversationRef,
        request: QuestionRequest,
    ) -> QuestionResponse:
        if self._question_channel is None:
            return QuestionResponse(
                decision="unavailable",
                feedback="question channel is not available",
            )
        return await self._question_channel.request_question(request, ref=ref)

    async def upload_file(self, ref: ConversationRef, path: Path) -> SendResult:
        del ref
        return SendResult(ok=False, error=f"web upload not supported: {path}")
