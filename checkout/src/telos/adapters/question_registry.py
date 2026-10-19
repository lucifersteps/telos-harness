"""Process-level pending user-question registry."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from telos.adapters.base import ConversationRef
from telos.adapters.question_channel import QuestionRequest, QuestionResponse
from telos.conversation.controller import ConversationController
from telos.conversation.repository import ConversationRepository
from telos.conversation.types import (
    ConversationInput,
    ConversationInputKind,
    ConversationInputRequestKind,
    ConversationInputRequestStatus,
    ConversationMessageRecord,
)
from telos.core.ids import generate_id


@dataclass(frozen=True)
class PendingQuestionEntry:
    request_id: str
    session_id: str
    turn_id: str
    tool_call_id: str
    question: str
    future: asyncio.Future[QuestionResponse]


class PendingQuestionRegistry:
    """Pending ask_user futures keyed by durable input request id."""

    def __init__(self, repository: ConversationRepository) -> None:
        self._repository = repository
        self._pending: dict[str, PendingQuestionEntry] = {}

    def register(self, request: QuestionRequest) -> tuple[str, asyncio.Future[QuestionResponse]]:
        loop = asyncio.get_running_loop()
        request_id = str(request.request_id or "").strip() or generate_id("ques")
        future: asyncio.Future[QuestionResponse] = loop.create_future()
        session_id = str(request.session_id or "").strip()
        turn_id = str(request.turn_id or "").strip()
        tool_call_id = str(request.tool_call_id or "").strip()
        tool_record_id = ""
        if session_id and tool_call_id:
            tool_call = self._repository.get_tool_call(
                session_id=session_id,
                tool_call_id=tool_call_id,
            )
            if tool_call is not None:
                tool_record_id = tool_call.tool_record_id
        self._repository.create_input_request(
            request_id=request_id,
            session_id=session_id,
            kind=ConversationInputRequestKind.QUESTION,
            title="用户追问",
            prompt=request.question,
            source_type="ask_user",
            source_id=tool_call_id,
            turn_id=turn_id,
            tool_record_id=tool_record_id,
            tool_call_id=tool_call_id,
            detail={
                "question": request.question,
                "options": list(request.options or []),
                "allow_free_text": bool(request.allow_free_text),
                **dict(request.detail or {}),
            },
            metadata={
                "tool_name": request.tool_name,
                "timeout_seconds": float(request.timeout_seconds or 0),
            },
        )
        self._pending[request_id] = PendingQuestionEntry(
            request_id=request_id,
            session_id=session_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            question=request.question,
            future=future,
        )
        return request_id, future

    def resolve(
        self,
        request_id: str,
        *,
        decision: str = "answered",
        answer: str = "",
        selected_options: list[str] | None = None,
        feedback: str = "",
        response: dict[str, Any] | None = None,
    ) -> bool:
        clean_request_id = str(request_id or "").strip()
        if not clean_request_id:
            return False
        entry = self._pending.get(clean_request_id)
        if entry is None or entry.future.done():
            return False
        normalized = _normalize_decision(decision)
        selected = [str(item) for item in list(selected_options or []) if str(item)]
        payload = {
            **dict(response or {}),
            "answer": str(answer or ""),
            "selected_options": selected,
            "feedback": str(feedback or ""),
        }
        stored = self._repository.get_input_request(clean_request_id)
        if (
            stored is not None
            and stored.status == ConversationInputRequestStatus.PENDING
        ):
            status = (
                ConversationInputRequestStatus.CANCELLED
                if normalized in {"cancelled", "denied"}
                else ConversationInputRequestStatus.RESOLVED
            )
            self._repository.resolve_input_request(
                clean_request_id,
                status=status,
                decision=normalized,
                response=payload,
            )
        self._pending.pop(clean_request_id, None)
        entry.future.set_result(
            QuestionResponse(
                decision=normalized,
                answer=str(answer or ""),
                selected_options=selected,
                feedback=str(feedback or ""),
                message_time=str(payload.get("message_time") or ""),
            )
        )
        return True

    def answer_image_question_from_queue(
        self, request_id: str, *, ref: ConversationRef,
    ) -> ConversationMessageRecord | None:
        entry = self._pending.get(request_id)
        if entry is None or entry.future.done():
            return None
        message = self._repository.answer_image_question_from_queue(request_id, ref=ref)
        if message is not None:
            self.resolve(request_id, answer=message.text, response={
                "message_time": message.created_at, "message_id": message.message_id,
            })
        return message

    def expire(self, request_id: str, feedback: str = "question timed out") -> bool:
        clean_request_id = str(request_id or "").strip()
        if not clean_request_id:
            return False
        self._repository.resolve_input_request(
            clean_request_id,
            status=ConversationInputRequestStatus.EXPIRED,
            decision="timeout",
            response={"feedback": feedback},
        )
        entry = self._pending.pop(clean_request_id, None)
        if entry is None or entry.future.done():
            return False
        entry.future.set_result(
            QuestionResponse(decision="timeout", feedback=feedback)
        )
        return True

    def cancel_by_turn(self, turn_id: str, feedback: str = "cancelled by stop") -> int:
        cancelled = 0
        for entry in self.pending_for_turn(turn_id):
            stored = self._repository.get_input_request(entry.request_id)
            if (
                stored is not None
                and stored.status == ConversationInputRequestStatus.PENDING
            ):
                self._repository.resolve_input_request(
                    entry.request_id,
                    status=ConversationInputRequestStatus.CANCELLED,
                    decision="cancelled",
                    response={"feedback": feedback},
                )
            if entry.future.done():
                continue
            entry.future.set_result(
                QuestionResponse(decision="cancelled", feedback=feedback)
            )
            self._pending.pop(entry.request_id, None)
            cancelled += 1
        return cancelled

    def pending_for_turn(self, turn_id: str) -> list[PendingQuestionEntry]:
        clean_turn_id = str(turn_id or "").strip()
        if not clean_turn_id:
            return []
        return [
            entry
            for entry in list(self._pending.values())
            if entry.turn_id == clean_turn_id and not entry.future.done()
        ]

    def discard(self, request_id: str) -> None:
        self._pending.pop(str(request_id or "").strip(), None)

    def pending_ids(self) -> list[str]:
        return [
            request_id
            for request_id, entry in self._pending.items()
            if not entry.future.done()
        ]


class RegistryQuestionChannel:
    def __init__(
        self,
        registry: PendingQuestionRegistry,
        *,
        conversation_controller: ConversationController | None = None,
        source: str = "",
        surface: str = "",
    ) -> None:
        self._registry = registry
        self._conversation_controller = conversation_controller
        self._source = str(source or "")
        self._surface = str(surface or "")

    async def request_question(
        self,
        request: QuestionRequest,
        *,
        ref: ConversationRef | None = None,
    ) -> QuestionResponse:
        if not str(request.session_id or "").strip():
            return QuestionResponse(
                decision="unavailable",
                feedback="missing conversation session id",
            )
        request_id, future = self._registry.register(request)
        try:
            timeout = float(request.timeout_seconds or 0)
            if timeout > 0:
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(future),
                        timeout=timeout,
                    )
                except TimeoutError:
                    feedback = f"question timed out after {int(timeout)} seconds"
                    if self._record_timeout_input(
                        request,
                        request_id=request_id,
                        ref=ref,
                        feedback=feedback,
                    ):
                        self._registry.discard(request_id)
                        if not future.done():
                            future.set_result(
                                QuestionResponse(
                                    decision="timeout",
                                    feedback=feedback,
                                )
                            )
                    else:
                        self._registry.expire(request_id, feedback=feedback)
                    return QuestionResponse(
                        decision="timeout",
                        feedback=feedback,
                    )
            return await future
        finally:
            self._registry.discard(request_id)

    def _record_timeout_input(
        self,
        request: QuestionRequest,
        *,
        request_id: str,
        ref: ConversationRef | None,
        feedback: str,
    ) -> bool:
        controller = self._conversation_controller
        if controller is None or ref is None:
            return False
        controller.submit_input(
            ConversationInput(
                ref=ref,
                text="timeout",
                raw_event_id=f"question-timeout:{request_id}",
                source=self._source,
                surface=self._surface,
                metadata={
                    "input_kind": ConversationInputKind.QUESTION_RESPONSE.value,
                    "conversation_session_id": str(request.session_id or ""),
                    "input_request_id": str(request_id or ""),
                    "input_request_kind": ConversationInputRequestKind.QUESTION.value,
                    "tool_call_id": str(request.tool_call_id or ""),
                    "decision": "timeout",
                    "feedback": str(feedback or ""),
                    "question_timeout": True,
                },
            )
        )
        return True


def _normalize_decision(value: str) -> str:
    text = str(value or "").strip().lower()
    if text in {"answer", "answered", "submit", "confirm", "yes", "ok"}:
        return "answered"
    if text in {"cancel", "cancelled", "canceled"}:
        return "cancelled"
    if text in {"deny", "denied", "reject", "rejected", "no"}:
        return "denied"
    return text or "answered"
