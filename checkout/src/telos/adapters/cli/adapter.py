"""CLI adapter implementation."""

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
from telos.adapters.cli.renderer import print_text


class CliAdapter:
    name = "cli"
    capabilities = AdapterCapabilities(
        max_text_chars=20_000,
        supports_buttons=False,
        supports_threads=False,
        supports_files=False,
    )

    def __init__(self, *, user_id: str = "local", session_name: str = "default"):
        self.user_id = user_id
        self.session_name = session_name

    def make_ref(self) -> ConversationRef:
        return ConversationRef(
            platform="cli",
            chat_id=self.session_name,
            user_id=self.user_id,
            source_id=self.session_name,
        )

    def make_turn(self, text: str) -> InboundTurn:
        return InboundTurn(ref=self.make_ref(), text=text)

    async def receive(self) -> AsyncIterator[InboundTurn]:
        if False:
            yield self.make_turn("")
        return

    def drain_recorded_inputs(self) -> list[InboundTurn]:
        return []

    async def send_text(self, ref: ConversationRef, text: str) -> SendResult:
        del ref
        print_text(text)
        return SendResult(ok=True)

    async def request_approval(
        self,
        ref: ConversationRef,
        request: ApprovalRequest,
    ) -> ApprovalResponse:
        del ref
        print_text(
            f"{request.tool_name} wants to {request.action}. "
            "Type 1 to approve, anything else to cancel:"
        )
        decision = input().strip()
        return ApprovalResponse(decision="approve" if decision == "1" else "deny")

    async def upload_file(self, ref: ConversationRef, path: Path) -> SendResult:
        del ref
        return SendResult(ok=False, error=f"CLI upload is not implemented: {path}")
