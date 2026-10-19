"""Classify and record user replies to pending questions."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

from telos.adapters.base import ConversationRef, InboundTurn
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from telos.adapters.question_registry import PendingQuestionRegistry
if TYPE_CHECKING:
    from telos.conversation.controller import ConversationController
from telos.conversation.types import ConversationInput


@dataclass(frozen=True)
class QuestionReply:
    answer: str
    selected_options: list[str]


def classify_question_reply(
    text: str,
    *,
    options: list[str],
    allow_free_text: bool,
    allow_slash_text: bool = False,
) -> QuestionReply | None:
    value = str(text or "").strip()
    if not value:
        return None
    clean_options = [str(option).strip() for option in list(options or []) if str(option).strip()]
    if not allow_slash_text and value.startswith("/") and value not in clean_options:
        return None
    selected = _selected_option(value, clean_options)
    if selected:
        return QuestionReply(answer=selected, selected_options=[selected])
    if clean_options and not allow_free_text:
        return None
    if not allow_free_text:
        return None
    return QuestionReply(answer=value, selected_options=[])


def question_response_turn(
    turn: InboundTurn,
    *,
    request_id: str = "",
    pending_request_ids: list[str] | None = None,
    tool_call_id: str = "",
    decision: str,
    answer: str = "",
    selected_options: list[str] | None = None,
    feedback: str = "",
    runtime_missing: bool = False,
    stop_cascade: bool = False,
    question_timeout: bool = False,
) -> InboundTurn:
    metadata = dict(turn.metadata or {})
    pending_ids = [str(item) for item in list(pending_request_ids or []) if str(item)]
    metadata.update(
        {
            "input_kind": "question_response",
            "input_request_id": str(request_id or ""),
            "pending_request_ids": pending_ids,
            "tool_call_id": str(tool_call_id or ""),
            "decision": str(decision or ""),
            "answer": str(answer or ""),
            "selected_options": [
                str(item) for item in list(selected_options or []) if str(item)
            ],
            "feedback": str(feedback or ""),
            "question_runtime_missing": bool(runtime_missing),
            "stop_cascade": bool(stop_cascade),
            "question_timeout": bool(question_timeout),
        }
    )
    return replace(turn, metadata=metadata)


def question_cancelled_by_stop_input(
    ref: ConversationRef,
    *,
    request_id: str,
    session_id: str,
    tool_call_id: str = "",
    source: str = "",
    surface: str = "",
    feedback: str = "cancelled by stop",
) -> ConversationInput:
    clean_request_id = str(request_id or "").strip()
    return ConversationInput(
        ref=ref,
        text="/stop",
        raw_event_id=f"question-stop:{clean_request_id}:cancelled",
        source=str(source or ""),
        surface=str(surface or ""),
        metadata={
            "input_kind": "question_response",
            "conversation_session_id": str(session_id or ""),
            "input_request_id": clean_request_id,
            "input_request_kind": "question",
            "tool_call_id": str(tool_call_id or ""),
            "decision": "cancelled",
            "feedback": str(feedback or ""),
            "question_runtime_missing": False,
            "stop_cascade": True,
        },
    )


def cancel_pending_questions_by_stop(
    question_registry: PendingQuestionRegistry | None,
    conversation_controller: ConversationController,
    *,
    ref: ConversationRef,
    turn_ids: Iterable[str],
    session_id: str = "",
    source: str = "",
    surface: str = "",
    feedback: str = "cancelled by stop",
) -> int:
    if question_registry is None:
        return 0
    cancelled = 0
    seen: set[str] = set()
    for turn_id in turn_ids:
        for entry in question_registry.pending_for_turn(turn_id):
            if entry.request_id in seen:
                continue
            seen.add(entry.request_id)
            request_session_id = entry.session_id or str(session_id or "")
            conversation_controller.submit_input(
                question_cancelled_by_stop_input(
                    ref,
                    request_id=entry.request_id,
                    session_id=request_session_id,
                    tool_call_id=entry.tool_call_id,
                    source=source,
                    surface=surface,
                    feedback=feedback,
                )
            )
            if question_registry.resolve(
                entry.request_id,
                decision="cancelled",
                feedback=feedback,
            ):
                cancelled += 1
    return cancelled


def _selected_option(value: str, options: list[str]) -> str:
    if not options:
        return ""
    try:
        index = int(value)
    except ValueError:
        index = 0
    if 1 <= index <= len(options):
        return options[index - 1]
    lowered = value.lower()
    for option in options:
        if lowered == option.lower():
            return option
    return ""
