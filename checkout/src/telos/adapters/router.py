"""Adapter wrapper that routes outbound calls by conversation platform."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from pathlib import Path

from telos.adapters.base import (
    Adapter,
    AdapterCapabilities,
    ApprovalRequest,
    ApprovalResponse,
    ConversationRef,
    InboundTurn,
    SendResult,
)
from telos.adapters.question_channel import QuestionRequest, QuestionResponse


class OutboundRouterAdapter:
    """Use one adapter for inbound turns and platform adapters for outbound delivery."""

    def __init__(self, *, inbound: Adapter, outbound_adapters: Iterable[Adapter]):
        self.inbound = inbound
        self.name = inbound.name
        self.capabilities: AdapterCapabilities = inbound.capabilities
        self.outbound_by_platform = {
            adapter.name: adapter
            for adapter in outbound_adapters
            if adapter.name != inbound.name
        }

    async def receive(self) -> AsyncIterator[InboundTurn]:
        async for turn in self.inbound.receive():
            yield turn

    def drain_recorded_inputs(self) -> list[InboundTurn]:
        return self.inbound.drain_recorded_inputs()

    async def send_text(self, ref: ConversationRef, text: str) -> SendResult:
        return await self._adapter_for(ref).send_text(ref, text)

    async def request_approval(
        self,
        ref: ConversationRef,
        request: ApprovalRequest,
    ) -> ApprovalResponse:
        if _is_scheduler_adapter(self.inbound):
            return await self.inbound.request_approval(ref, request)
        return await self._adapter_for(ref).request_approval(ref, request)

    async def request_question(
        self,
        ref: ConversationRef,
        request: QuestionRequest,
    ) -> QuestionResponse:
        handler = getattr(self._adapter_for(ref), "request_question", None)
        if not callable(handler):
            return QuestionResponse(
                decision="unavailable",
                feedback="question channel is not available",
            )
        return await handler(ref, request)

    async def upload_file(self, ref: ConversationRef, path: Path) -> SendResult:
        return await self._adapter_for(ref).upload_file(ref, path)

    def _adapter_for(self, ref: ConversationRef) -> Adapter:
        return self.outbound_by_platform.get(ref.platform) or self.inbound


def _is_scheduler_adapter(adapter: Adapter) -> bool:
    return str(getattr(adapter, "name", "") or "").strip().lower() == "scheduler"
