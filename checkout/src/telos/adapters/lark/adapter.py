"""Lark adapter implementation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

from telos.adapters.base import (
    AdapterCapabilities,
    ApprovalRequest,
    ApprovalResponse,
    ConversationRef,
    InboundTurn,
    SendResult,
)
from telos.adapters.approval_channel import SharedApprovalRegistry
from telos.adapters.approval_reply import approval_response_turn, classify_approval_reply
from telos.adapters.question_channel import (
    QuestionRequest,
    QuestionResponse,
    SharedQuestionRegistry,
)
from telos.adapters.question_reply import classify_question_reply, question_response_turn
from telos.adapters.lark.auth import (
    authorize_lark_event,
    is_lark_url_verification,
    lark_url_verification_response,
)
from telos.adapters.lark.cards import build_approval_card
from telos.adapters.lark.config import LarkConfig
from telos.adapters.lark.inbound import normalize_lark_event
from telos.adapters.lark.outbound import LarkOutbound, SendTextFn, create_lark_send_text_fn
from telos.core.ids import generate_id


class LarkAdapter:
    name = "lark"

    def __init__(
        self,
        *,
        config: LarkConfig | None = None,
        send_text_fn: SendTextFn | None = None,
        approval_registry: SharedApprovalRegistry | None = None,
        question_registry: SharedQuestionRegistry | None = None,
    ):
        self.config = config or LarkConfig.from_env()
        self._approval_registry = approval_registry
        self._question_registry = question_registry
        self.capabilities = AdapterCapabilities(
            max_text_chars=self.config.max_text_chars,
            supports_buttons=True,
            supports_threads=True,
            supports_files=False,
        )
        self._inbox: list[InboundTurn] = []
        self.rejected_events: list[tuple[dict, str]] = []
        self._recorded_inputs: list[InboundTurn] = []
        self._approval_futures: dict[str, asyncio.Future[ApprovalResponse]] = {}
        self._approval_refs: dict[str, ConversationRef] = {}
        self._approval_request_ids: dict[str, str] = {}
        self._question_futures: dict[str, asyncio.Future[QuestionResponse]] = {}
        self._question_refs: dict[str, ConversationRef] = {}
        self._question_request_ids: dict[str, str] = {}
        self._question_requests: dict[str, QuestionRequest] = {}
        self.approval_cards: list[dict] = []
        self.outbound = LarkOutbound(
            config=self.config,
            send_text_fn=send_text_fn or create_lark_send_text_fn(self.config),
        )

    def push_raw_event(self, raw: dict) -> InboundTurn | None:
        decision = authorize_lark_event(raw, self.config)
        if not decision.allowed:
            self.rejected_events.append((raw, decision.reason))
            return normalize_lark_event(raw)
        if is_lark_url_verification(raw):
            try:
                lark_url_verification_response(raw, self.config)
            except ValueError as exc:
                self.rejected_events.append((raw, str(exc)))
                return normalize_lark_event(raw)
            self.rejected_events.append((raw, "url verification"))
            return normalize_lark_event(raw)
        turn = self._with_adapter_identity(normalize_lark_event(raw))
        if self._consume_approval_reply(turn):
            return None
        if self._consume_question_reply(turn):
            return None
        self._inbox.append(turn)
        return turn

    def handle_url_verification(self, raw: dict) -> dict[str, str] | None:
        return lark_url_verification_response(raw, self.config)

    async def receive(self) -> AsyncIterator[InboundTurn]:
        while self._inbox:
            yield self._inbox.pop(0)

    def drain_recorded_inputs(self) -> list[InboundTurn]:
        recorded = list(self._recorded_inputs)
        self._recorded_inputs.clear()
        return recorded

    async def send_text(self, ref: ConversationRef, text: str) -> SendResult:
        return await self.outbound.send_text(ref, text)

    async def request_approval(
        self,
        ref: ConversationRef,
        request: ApprovalRequest,
    ) -> ApprovalResponse:
        self.approval_cards.append(build_approval_card(request))
        target_hint = str(request.tool_call_id or request.request_id or "").strip()
        suffix = f" Approval id: {target_hint}." if target_hint else ""
        future: asyncio.Future[ApprovalResponse] = asyncio.get_running_loop().create_future()
        self._approval_futures[request.tool_call_id] = future
        self._approval_refs[request.tool_call_id] = ref
        if request.request_id:
            self._approval_request_ids[request.tool_call_id] = request.request_id
        registry_future: asyncio.Future[ApprovalResponse] | None = None
        if self._approval_registry is not None and request.request_id:
            registry_future = self._approval_registry.register(request)
        try:
            delivery = await self.send_text(
                ref,
                f"{request.tool_name} requires approval. Use the approval card or reply 1.{suffix}",
            )
            if not delivery.ok:
                feedback = delivery.error or "approval message delivery failed"
                if registry_future is not None and not registry_future.done():
                    registry_future.set_result(
                        ApprovalResponse(decision="unavailable", feedback=feedback)
                    )
                future.set_result(
                    ApprovalResponse(decision="unavailable", feedback=feedback)
                )
                self._recorded_inputs.append(
                    approval_response_turn(
                        InboundTurn(
                            ref=ref,
                            text="unavailable",
                            raw_event_id=f"approval-delivery:{request.request_id}:unavailable",
                        ),
                        request_id=request.request_id,
                        tool_call_id=request.tool_call_id,
                        decision="unavailable",
                        feedback=feedback,
                        runtime_resolved=True,
                    )
                )
                return await future
            if registry_future is None:
                return await future
            done, _pending = await asyncio.wait(
                {future, registry_future},
                return_when=asyncio.FIRST_COMPLETED,
            )
            return next(iter(done)).result()
        finally:
            self._approval_futures.pop(request.tool_call_id, None)
            self._approval_refs.pop(request.tool_call_id, None)
            self._approval_request_ids.pop(request.tool_call_id, None)
            if registry_future is not None and request.request_id:
                self._approval_registry.discard(request.request_id)

    def resolve_approval(self, tool_call_id: str, decision: str) -> bool:
        future = self._approval_futures.get(tool_call_id)
        if future is None or future.done():
            return False
        normalized = decision.strip().lower()
        reply = classify_approval_reply(normalized)
        approval_decision = (
            str(reply.decision or "").strip()
            if reply is not None
            else "approve"
            if normalized in {"approve", "approved", "1", "yes"}
            else "deny"
        )
        approved = approval_decision == "approve"
        future.set_result(
            ApprovalResponse(
                decision=approval_decision,
                feedback="" if approved else _approval_feedback_for_decision(approval_decision),
            )
        )
        request_id = self._approval_request_ids.get(tool_call_id)
        if self._approval_registry is not None and request_id:
            self._approval_registry.discard(request_id)
        return True

    def cancel_approval(
        self,
        tool_call_id: str,
        feedback: str = "cancelled by stop",
        *,
        resolve_registry: bool = True,
    ) -> bool:
        future = self._approval_futures.get(tool_call_id)
        resolved = False
        request_id = self._approval_request_ids.get(tool_call_id)
        if future is None or future.done():
            pass
        else:
            future.set_result(ApprovalResponse(decision="cancelled", feedback=feedback))
            resolved = True
        if resolve_registry and self._approval_registry is not None and request_id:
            resolved = (
                self._approval_registry.resolve(request_id, "cancelled", feedback)
                or resolved
            )
        elif self._approval_registry is not None and request_id:
            self._approval_registry.discard(request_id)
        return resolved

    async def request_question(
        self,
        ref: ConversationRef,
        request: QuestionRequest,
    ) -> QuestionResponse:
        request = _question_request_with_id(request)
        future: asyncio.Future[QuestionResponse] = asyncio.get_running_loop().create_future()
        self._question_futures[request.tool_call_id] = future
        self._question_refs[request.tool_call_id] = ref
        self._question_request_ids[request.tool_call_id] = request.request_id
        self._question_requests[request.tool_call_id] = request
        registry_future: asyncio.Future[QuestionResponse] | None = None
        if self._question_registry is not None:
            _request_id, registry_future = self._question_registry.register(request)
        try:
            delivery = await self.send_text(ref, _question_message_text(request))
            if not delivery.ok:
                feedback = delivery.error or "question message delivery failed"
                if registry_future is not None and not registry_future.done():
                    registry_future.set_result(
                        QuestionResponse(decision="unavailable", feedback=feedback)
                    )
                self._recorded_inputs.append(
                    question_response_turn(
                        InboundTurn(
                            ref=ref,
                            text="unavailable",
                            raw_event_id=f"question-delivery:{request.request_id}:unavailable",
                        ),
                        request_id=request.request_id,
                        tool_call_id=request.tool_call_id,
                        decision="unavailable",
                        feedback=feedback,
                    )
                )
                return QuestionResponse(decision="unavailable", feedback=feedback)
            response = await _wait_for_question_response(
                local_future=future,
                registry_future=registry_future,
                timeout_seconds=request.timeout_seconds,
            )
            if response.decision == "timeout":
                self._recorded_inputs.append(
                    question_response_turn(
                        InboundTurn(
                            ref=ref,
                            text="timeout",
                            raw_event_id=f"question-timeout:{request.request_id}",
                        ),
                        request_id=request.request_id,
                        tool_call_id=request.tool_call_id,
                        decision="timeout",
                        feedback=response.feedback,
                        question_timeout=True,
                    )
                )
            return response
        finally:
            self._question_futures.pop(request.tool_call_id, None)
            self._question_refs.pop(request.tool_call_id, None)
            self._question_request_ids.pop(request.tool_call_id, None)
            self._question_requests.pop(request.tool_call_id, None)
            if self._question_registry is not None and request.request_id:
                self._question_registry.discard(request.request_id)

    def resolve_question(
        self,
        tool_call_id: str,
        *,
        answer: str = "",
        selected_options: list[str] | None = None,
        decision: str = "answered",
        feedback: str = "",
        resolve_registry: bool = True,
    ) -> bool:
        selected = [str(item) for item in list(selected_options or []) if str(item)]
        response = QuestionResponse(
            decision=str(decision or "answered"),
            answer=str(answer or ""),
            selected_options=selected,
            feedback=str(feedback or ""),
        )
        resolved = False
        future = self._question_futures.get(tool_call_id)
        if future is not None and not future.done():
            future.set_result(response)
            resolved = True
        request_id = self._question_request_ids.get(tool_call_id)
        if resolve_registry and self._question_registry is not None and request_id:
            resolved = (
                self._question_registry.resolve(
                    request_id,
                    decision=response.decision,
                    answer=response.answer,
                    selected_options=response.selected_options,
                    feedback=response.feedback,
                )
                or resolved
            )
        elif self._question_registry is not None and request_id:
            self._question_registry.discard(request_id)
        return resolved

    def cancel_question(
        self,
        tool_call_id: str,
        feedback: str = "cancelled by stop",
        *,
        resolve_registry: bool = True,
    ) -> bool:
        if resolve_registry:
            return self.resolve_question(
                tool_call_id,
                decision="cancelled",
                feedback=feedback,
            )
        future = self._question_futures.get(tool_call_id)
        if future is None or future.done():
            return False
        future.set_result(
            QuestionResponse(
                decision="cancelled",
                feedback=feedback,
            )
        )
        request_id = self._question_request_ids.get(tool_call_id)
        if self._question_registry is not None and request_id:
            self._question_registry.discard(request_id)
        return True

    def _consume_question_reply(self, turn: InboundTurn) -> bool:
        tool_call_ids = self._pending_question_ids_for_turn(turn)
        if not tool_call_ids:
            persistent = _persistent_question_reply_from_text(turn.text)
            if persistent is None:
                return False
            request_id, decision, answer = persistent
            self._recorded_inputs.append(
                question_response_turn(
                    turn,
                    request_id=request_id,
                    decision=decision,
                    answer=answer,
                    feedback=(
                        "cancelled by lark question reply"
                        if decision == "cancelled"
                        else ""
                    ),
                    runtime_missing=True,
                )
            )
            return True
        if _is_stop_command_text(turn.text):
            for tool_call_id in tool_call_ids:
                request_id = str(self._question_request_ids.get(tool_call_id) or "")
                if self.cancel_question(tool_call_id, resolve_registry=False):
                    self._recorded_inputs.append(
                        question_response_turn(
                            turn,
                            request_id=request_id,
                            tool_call_id=tool_call_id,
                            decision="cancelled",
                            feedback="cancelled by stop",
                            stop_cascade=True,
                        )
                    )
            return False
        tool_call_id, answer_text, explicit_target = self._select_question_id_for_reply(
            turn.text,
            tool_call_ids,
        )
        if not tool_call_id:
            self._recorded_inputs.append(
                question_response_turn(
                    turn,
                    pending_request_ids=[
                        str(self._question_request_ids.get(tool_call_id) or "")
                        for tool_call_id in tool_call_ids
                    ],
                    decision="ambiguous",
                    feedback="multiple pending questions",
                )
            )
            return True
        request = self._question_requests.get(tool_call_id)
        if request is None:
            return False
        reply = classify_question_reply(
            answer_text,
            options=list(request.options or []),
            allow_free_text=bool(request.allow_free_text),
            allow_slash_text=explicit_target,
        )
        if reply is None:
            if explicit_target:
                self._recorded_inputs.append(
                    question_response_turn(
                        turn,
                        request_id=str(self._question_request_ids.get(tool_call_id) or ""),
                        tool_call_id=tool_call_id,
                        decision="invalid",
                        feedback="answer did not match question options",
                    )
                )
                return True
            return False
        resolved = self.resolve_question(
            tool_call_id,
            answer=reply.answer,
            selected_options=reply.selected_options,
            resolve_registry=False,
        )
        if resolved:
            self._recorded_inputs.append(
                question_response_turn(
                    turn,
                    request_id=str(self._question_request_ids.get(tool_call_id) or ""),
                    tool_call_id=tool_call_id,
                    decision="answered",
                    answer=reply.answer,
                    selected_options=reply.selected_options,
                )
            )
        return resolved

    def _pending_question_id_for_turn(self, turn: InboundTurn) -> str:
        ids = self._pending_question_ids_for_turn(turn)
        return ids[0] if len(ids) == 1 else ""

    def _pending_question_ids_for_turn(self, turn: InboundTurn) -> list[str]:
        ids: list[str] = []
        for tool_call_id, future in list(self._question_futures.items()):
            if future.done():
                continue
            ref = self._question_refs.get(tool_call_id)
            if ref is not None and _same_approval_ref(ref, turn.ref):
                ids.append(tool_call_id)
        return ids

    def _select_question_id_for_reply(
        self,
        text: str,
        tool_call_ids: list[str],
    ) -> tuple[str, str, bool]:
        raw = str(text or "").strip()
        if len(tool_call_ids) == 1:
            return tool_call_ids[0], raw, False
        target_id, _, answer_text = raw.partition(" ")
        target_id = target_id.strip()
        answer = answer_text.strip()
        if not answer:
            return "", raw, False
        matches = [
            tool_call_id
            for tool_call_id in tool_call_ids
            if tool_call_id == target_id
            or str(self._question_request_ids.get(tool_call_id) or "") == target_id
        ]
        return (matches[0], answer, True) if len(matches) == 1 else ("", raw, False)

    def _consume_approval_reply(self, turn: InboundTurn) -> bool:
        tool_call_ids = self._pending_approval_ids_for_turn(turn)
        reply = classify_approval_reply(turn.text)
        if not tool_call_ids:
            if reply is not None and _looks_like_approval_request_id(reply.target_id):
                decision = str(reply.decision or "").strip() or (
                    "approve" if reply.approved else "deny"
                )
                feedback = "" if reply.approved else _approval_feedback_for_decision(decision)
                self._recorded_inputs.append(
                    approval_response_turn(
                        turn,
                        request_id=reply.target_id,
                        decision=decision,
                        feedback=feedback,
                        runtime_missing=True,
                    )
                )
                return True
            return False
        if _is_stop_command_text(turn.text):
            for tool_call_id in tool_call_ids:
                request_id = str(self._approval_request_ids.get(tool_call_id) or "")
                if self.cancel_approval(tool_call_id, resolve_registry=False):
                    self._recorded_inputs.append(
                        approval_response_turn(
                            turn,
                            request_id=request_id,
                            tool_call_id=tool_call_id,
                            decision="cancelled",
                            feedback="cancelled by stop",
                            runtime_cancelled=True,
                            stop_cascade=True,
                        )
                    )
            return False
        if reply is None:
            return False
        tool_call_id = self._select_approval_id_for_reply(reply, tool_call_ids)
        if not tool_call_id:
            self._recorded_inputs.append(
                approval_response_turn(
                    turn,
                    pending_request_ids=[
                        str(self._approval_request_ids.get(tool_call_id) or "")
                        for tool_call_id in tool_call_ids
                    ],
                    decision="ambiguous",
                    feedback="multiple pending approvals",
                )
            )
            return True
        decision = str(reply.decision or "").strip() or (
            "approve" if reply.approved else "deny"
        )
        feedback = "" if reply.approved else _approval_feedback_for_decision(decision)
        resolved = self.resolve_approval(tool_call_id, turn.text)
        if resolved:
            self._recorded_inputs.append(
                approval_response_turn(
                    turn,
                    request_id=str(self._approval_request_ids.get(tool_call_id) or ""),
                    tool_call_id=tool_call_id,
                    decision=decision,
                    feedback=feedback,
                )
            )
        return resolved

    def _pending_approval_id_for_turn(self, turn: InboundTurn) -> str:
        ids = self._pending_approval_ids_for_turn(turn)
        return ids[0] if len(ids) == 1 else ""

    def _pending_approval_ids_for_turn(self, turn: InboundTurn) -> list[str]:
        ids: list[str] = []
        for tool_call_id, future in list(self._approval_futures.items()):
            if future.done():
                continue
            ref = self._approval_refs.get(tool_call_id)
            if ref is not None and _same_approval_ref(ref, turn.ref):
                ids.append(tool_call_id)
        return ids

    def _select_approval_id_for_reply(
        self,
        reply,
        tool_call_ids: list[str],
    ) -> str:
        target_id = str(getattr(reply, "target_id", "") or "").strip()
        if not target_id:
            return tool_call_ids[0] if len(tool_call_ids) == 1 else ""
        matches = [
            tool_call_id
            for tool_call_id in tool_call_ids
            if tool_call_id == target_id
            or str(self._approval_request_ids.get(tool_call_id) or "") == target_id
        ]
        return matches[0] if len(matches) == 1 else ""

    async def upload_file(self, ref: ConversationRef, path: Path) -> SendResult:
        del ref
        return SendResult(ok=False, error=f"Lark upload is not implemented: {path}")

    def _with_adapter_identity(self, turn: InboundTurn) -> InboundTurn:
        if not self.config.app_id:
            return turn
        return replace(turn, ref=replace(turn.ref, app_id=self.config.app_id))


def _is_stop_command_text(text: str) -> bool:
    return str(text or "").strip().lower() in {"/stop", "停止", "停下"}


def _approval_feedback_for_decision(decision: str) -> str:
    if str(decision or "").strip().lower() == "cancelled":
        return "cancelled by lark approval"
    return "denied by lark approval"


def _looks_like_approval_request_id(value: str) -> bool:
    return str(value or "").strip().startswith("appr_")


def _persistent_question_reply_from_text(text: str) -> tuple[str, str, str] | None:
    raw = str(text or "").strip()
    command, _, rest = raw.partition(" ")
    if command.strip().lower() in {"取消", "cancel", "cancelled"}:
        request_id, _, _trailing = rest.strip().partition(" ")
        request_id = request_id.strip()
        if request_id.startswith("ques_"):
            return request_id, "cancelled", ""
        return None
    request_id, _, answer = raw.partition(" ")
    request_id = request_id.strip()
    answer = answer.strip()
    if not request_id.startswith("ques_") or not answer:
        return None
    return request_id, "answered", answer


def _question_request_with_id(request: QuestionRequest) -> QuestionRequest:
    if str(request.request_id or "").strip():
        return request
    return replace(request, request_id=generate_id("ques"))


async def _wait_for_question_response(
    *,
    local_future: asyncio.Future[QuestionResponse],
    registry_future: asyncio.Future[QuestionResponse] | None,
    timeout_seconds: float,
) -> QuestionResponse:
    waitables: set[asyncio.Future[QuestionResponse]] = {local_future}
    if registry_future is not None:
        waitables.add(registry_future)
    timeout = float(timeout_seconds or 0)
    if timeout > 0:
        done, _pending = await asyncio.wait(
            waitables,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if done:
            return next(iter(done)).result()
        feedback = f"question timed out after {int(timeout)} seconds"
        if not local_future.done():
            local_future.set_result(QuestionResponse(decision="timeout", feedback=feedback))
        return QuestionResponse(decision="timeout", feedback=feedback)
    done, _pending = await asyncio.wait(
        waitables,
        return_when=asyncio.FIRST_COMPLETED,
    )
    return next(iter(done)).result()


def _question_message_text(request: QuestionRequest) -> str:
    lines = [f"需要你回答：{request.question}"]
    target_hint = str(request.tool_call_id or request.request_id or "").strip()
    if target_hint:
        lines.append(f"问题ID：{target_hint}")
    options = [str(option).strip() for option in list(request.options or []) if str(option).strip()]
    if options:
        lines.extend(f"{index}. {option}" for index, option in enumerate(options, start=1))
    if options and not request.allow_free_text:
        lines.append("请回复选项编号或选项文字。")
    elif options:
        lines.append("可回复选项编号、选项文字，或直接输入答案。")
    else:
        lines.append("请直接回复答案。")
    return "\n".join(lines)


def _same_approval_ref(left: ConversationRef, right: ConversationRef) -> bool:
    return (
        left.platform == right.platform
        and left.chat_id == right.chat_id
        and left.thread_id == right.thread_id
        and (not left.user_id or not right.user_id or left.user_id == right.user_id)
    )
