"""Adapter contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TYPE_CHECKING
from typing import AsyncIterator, Protocol

if TYPE_CHECKING:
    from telos.adapters.question_channel import QuestionRequest, QuestionResponse


@dataclass(frozen=True)
class ConversationRef:
    platform: str
    tenant_id: str = ""
    app_id: str = ""
    chat_id: str = ""
    thread_id: str = ""
    user_id: str = ""
    source_id: str = ""


@dataclass(frozen=True)
class AttachmentRef:
    kind: str
    uri: str
    name: str = ""
    mime_type: str = ""


@dataclass(frozen=True)
class InboundTurn:
    ref: ConversationRef
    text: str
    attachments: list[AttachmentRef] = field(default_factory=list)
    raw_event_id: str = ""
    is_group: bool = False
    mentions_bot: bool = False
    timestamp: datetime | None = None
    reply_to: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SendResult:
    ok: bool
    message_id: str = ""
    error: str = ""
    message_count: int = 0


@dataclass(frozen=True)
class ApprovalRequest:
    turn_id: str
    tool_call_id: str
    tool_name: str
    action: str
    detail: object | None = None
    timeout_seconds: float = 600.0
    #: Stable id minted by the policy layer, shared with the durable
    #: ``tool.call.approval_request`` event so any connection can answer by id.
    request_id: str = ""


@dataclass(frozen=True)
class ApprovalResponse:
    decision: str
    feedback: str = ""
    scope: str = ""
    modified_args: dict[str, Any] | None = None


@dataclass(frozen=True)
class AdapterCapabilities:
    max_text_chars: int = 2000
    supports_buttons: bool = False
    supports_threads: bool = False
    supports_files: bool = False


class Adapter(Protocol):
    name: str
    capabilities: AdapterCapabilities

    async def receive(self) -> AsyncIterator[InboundTurn]: ...

    def drain_recorded_inputs(self) -> list[InboundTurn]: ...

    async def send_text(self, ref: ConversationRef, text: str) -> SendResult: ...

    async def request_approval(
        self,
        ref: ConversationRef,
        request: ApprovalRequest,
    ) -> ApprovalResponse: ...

    async def request_question(
        self,
        ref: ConversationRef,
        request: "QuestionRequest",
    ) -> "QuestionResponse": ...

    async def upload_file(self, ref: ConversationRef, path: Path) -> SendResult: ...
