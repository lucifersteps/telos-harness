"""Adapter-specific user question channel helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from telos.adapters.base import Adapter, ConversationRef

if TYPE_CHECKING:
    from telos.conversation.types import ConversationMessageRecord


@dataclass(frozen=True)
class QuestionRequest:
    session_id: str
    turn_id: str
    tool_call_id: str
    tool_name: str
    question: str
    options: list[str] = field(default_factory=list)
    allow_free_text: bool = True
    timeout_seconds: float = 600.0
    request_id: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class QuestionResponse:
    decision: str
    answer: str = ""
    selected_options: list[str] = field(default_factory=list)
    feedback: str = ""
    # 补充消息的记录时间；未提供时由业务工具使用当前时间。
    message_time: str = ""


class QuestionChannel(Protocol):
    async def request_question(self, request: QuestionRequest) -> QuestionResponse: ...


class SharedQuestionRegistry(Protocol):
    def register(self, request: QuestionRequest) -> tuple[str, asyncio.Future[QuestionResponse]]: ...

    def resolve(
        self,
        request_id: str,
        *,
        decision: str = "answered",
        answer: str = "",
        selected_options: list[str] | None = None,
        feedback: str = "",
        response: dict[str, Any] | None = None,
    ) -> bool: ...

    def expire(self, request_id: str, feedback: str = "question timed out") -> bool: ...

    def discard(self, request_id: str) -> None: ...

    def answer_image_question_from_queue(
        self, request_id: str, *, ref: ConversationRef,
    ) -> ConversationMessageRecord | None: ...


@dataclass(frozen=True)
class AdapterQuestionChannel:
    adapter: Adapter
    ref: ConversationRef

    async def request_question(self, request: QuestionRequest) -> QuestionResponse:
        handler = getattr(self.adapter, "request_question", None)
        if not callable(handler):
            return QuestionResponse(
                decision="unavailable",
                feedback="question channel is not available",
            )
        return await handler(self.ref, request)
