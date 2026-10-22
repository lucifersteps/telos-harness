"""微信收发与交互路由；单条待审批期间的文字反馈使旧预览失效并交回 Agent。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
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
from telos.adapters.inbox import AdapterInboxItem
from telos.adapters.delivery import delivery_fingerprint
from telos.adapters.wechat.auth import authorize_wechat_turn
from telos.adapters.wechat.config import WeChatConfig
from telos.adapters.wechat.inbound import normalize_wechat_event
from telos.adapters.wechat.media import MediaResolverFn
from telos.adapters.wechat.outbound import SendTextFn, WeChatOutbound, create_wechat_send_text_fn
from telos.adapters.question_channel import (
    QuestionRequest,
    QuestionResponse,
    SharedQuestionRegistry,
)
from telos.adapters.question_reply import classify_question_reply, question_response_turn
from telos.adapters.wechat.polling import (
    GetUpdatesFn,
    WeChatPollResult,
    create_wechat_get_updates_fn,
)
from telos.adapters.wechat.state import WeChatStateStore
from telos.core.clock import parse_user_time, to_user_time, user_now, user_time_iso
from telos.core.ids import generate_id

DEFAULT_IMAGE_PROMPT = ""
ClockFn = Callable[[], float]
WallClockFn = Callable[[], datetime]
logger = logging.getLogger(__name__)


@dataclass
class PendingMediaBatch:
    turn: InboundTurn
    deadline: float


class WeChatAdapter:
    name = "wechat"

    def __init__(
        self,
        *,
        config: WeChatConfig | None = None,
        send_text_fn: SendTextFn | None = None,
        get_updates_fn: GetUpdatesFn | None = None,
        state_store: WeChatStateStore | None = None,
        clock_fn: ClockFn | None = None,
        wall_clock_fn: WallClockFn | None = None,
        media_resolver: MediaResolverFn | None = None,
        approval_registry: SharedApprovalRegistry | None = None,
        question_registry: SharedQuestionRegistry | None = None,
    ):
        self.state_store = state_store
        # 共享待审批登记处（web 传入）：设了它，微信审批的 Future 也登记到这里，
        # 于是同一条审批可在微信或任一 web 端作答，先到者兑现。未设则退回本地 Future。
        self._approval_registry = approval_registry
        self._question_registry = question_registry
        self.config = _config_with_state(config or WeChatConfig.from_env(), self.state_store)
        self.clock_fn = clock_fn or _monotonic
        self.wall_clock_fn = wall_clock_fn or user_now
        self._sync_buf = ""
        self._long_poll_timeout_ms = 35_000
        self.capabilities = AdapterCapabilities(
            max_text_chars=self.config.max_text_chars,
            supports_buttons=False,
            supports_threads=False,
            supports_files=False,
        )
        self._inbox: list[InboundTurn] = []
        self._seen_event_ids: set[str] = set()
        self._seen_event_order: deque[str] = deque()
        self._pending_media: dict[str, PendingMediaBatch] = {}
        self.rejected_turns: list[tuple[InboundTurn, str]] = []
        self._recorded_inputs: list[InboundTurn] = []
        self._approval_futures: dict[str, asyncio.Future[ApprovalResponse]] = {}
        self._approval_refs: dict[str, ConversationRef] = {}
        self._approval_request_ids: dict[str, str] = {}
        self._question_futures: dict[str, asyncio.Future[QuestionResponse]] = {}
        self._question_refs: dict[str, ConversationRef] = {}
        self._question_request_ids: dict[str, str] = {}
        self._question_requests: dict[str, QuestionRequest] = {}
        self._get_updates = get_updates_fn or create_wechat_get_updates_fn(self.config)
        self._media_resolver = media_resolver
        self.outbound = WeChatOutbound(
            config=self.config,
            send_text_fn=send_text_fn
            or create_wechat_send_text_fn(
                self.config,
                context_token_provider=self._context_token_for_ref,
                context_token_clearer=self._clear_context_token_for_ref,
            ),
        )

    def push_raw_event(self, raw: dict) -> InboundTurn | None:
        return self._ingest_raw_event(raw, enqueue=True, buffer_media=True)

    def accept_raw_event(self, raw: dict) -> InboundTurn | None:
        """先授权与接收，个人调度器立即落盘并打断旧轮；媒体在可取消的任务内下载。"""
        return self._ingest_raw_event(raw, enqueue=False, buffer_media=False)

    async def resolve_turn_media(self, turn: InboundTurn, raw: dict) -> InboundTurn:
        message = await self._resolve_media(raw)
        resolved = normalize_wechat_event(message, account_id=self.config.account_id)
        return replace(turn, text=resolved.text, attachments=resolved.attachments,
                       metadata={**turn.metadata, **resolved.metadata})

    async def close_media(self) -> None:
        close = getattr(self._media_resolver, "close", None)
        if close:
            await close()

    async def prepare_raw_event(self, raw: dict) -> InboundTurn | None:
        message = await self._resolve_media(raw)
        return self._ingest_raw_event(message, enqueue=False, buffer_media=False)

    def _ingest_raw_event(
        self,
        raw: dict,
        *,
        enqueue: bool,
        buffer_media: bool,
    ) -> InboundTurn | None:
        turn = normalize_wechat_event(raw, account_id=self.config.account_id)
        decision = authorize_wechat_turn(turn, self.config)
        if not decision.allowed:
            self.rejected_turns.append((turn, decision.reason))
            return None
        if not self._mark_event_seen(turn):
            self.rejected_turns.append((turn, "duplicate event"))
            return None
        self._remember_context_token(raw, turn)
        if self._consume_approval_reply(turn):
            return None
        if self._consume_question_reply(turn):
            return None
        turn = self._maybe_merge_pending_media(turn)
        if buffer_media and self._should_buffer_media(turn):
            self._buffer_media(turn)
            return None
        if enqueue:
            self._inbox.append(turn)
        return turn

    async def poll_once(self, timeout_ms: int | None = None) -> WeChatPollResult:
        result = self._get_updates(self._sync_buf, timeout_ms or self._long_poll_timeout_ms)
        if hasattr(result, "__await__"):
            result = await result  # type: ignore[assignment]
        if result.suggested_timeout_ms > 0:
            self._long_poll_timeout_ms = result.suggested_timeout_ms
        return result

    def cursor_scope(self) -> dict[str, str]:
        return {
            "account_id": self.config.account_id or "default",
            "cursor_name": "get_updates_buf",
        }

    def set_sync_buf(self, sync_buf: str) -> None:
        self._sync_buf = str(sync_buf or "")

    def commit_poll_result(self, result: WeChatPollResult) -> None:
        if result.ok and result.sync_buf:
            self._sync_buf = result.sync_buf

    async def receive(self) -> AsyncIterator[InboundTurn]:
        self.flush_expired_media()
        while self._inbox:
            yield self._inbox.pop(0)

    def drain_recorded_inputs(self) -> list[InboundTurn]:
        recorded = list(self._recorded_inputs)
        self._recorded_inputs.clear()
        return recorded

    async def send_text(self, ref: ConversationRef, text: str, *, protected_text: str = "") -> SendResult:
        return await self.outbound.send_text(ref, text, protected_text=protected_text)

    async def request_approval(
        self,
        ref: ConversationRef,
        request: ApprovalRequest,
    ) -> ApprovalResponse:
        message = "\n\n".join(_approval_message_bubbles(request))
        future: asyncio.Future[ApprovalResponse] = asyncio.get_running_loop().create_future()
        self._approval_futures[request.tool_call_id] = future
        self._approval_refs[request.tool_call_id] = ref
        if request.request_id:
            self._approval_request_ids[request.tool_call_id] = request.request_id
        self._record_pending_approval(ref, request)
        # 同一条审批也登记进共享 registry（web 可按 request_id 作答）。两个 Future 谁先
        # 兑现谁生效：微信文字回复兑现本地 future，web 兑现 registry future。
        registry_future: asyncio.Future[ApprovalResponse] | None = None
        if self._approval_registry is not None and request.request_id:
            registry_future = self._approval_registry.register(request)
        try:
            delivery = await self.send_text(ref, message, protected_text=message)
            if not delivery.ok:
                feedback = delivery.error or "approval message delivery failed"
                if registry_future is not None and not registry_future.done():
                    registry_future.set_result(
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
                return ApprovalResponse(decision="unavailable", feedback=feedback)
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
            self._clear_pending_approval(request.tool_call_id)
            if registry_future is not None and request.request_id:
                self._approval_registry.discard(request.request_id)

    def resolve_approval(self, tool_call_id: str, reply_text: str) -> bool:
        reply = classify_approval_reply(reply_text)
        if reply is None:
            return False
        decision = reply.decision
        return self._resolve_approval_response(
            tool_call_id,
            ApprovalResponse(
                decision=decision,
                feedback="" if reply.approved else _approval_feedback_for_decision(decision),
            ),
        )

    def _resolve_approval_response(self, tool_call_id: str, response: ApprovalResponse) -> bool:
        future = self._approval_futures.get(tool_call_id)
        if future is None or future.done():
            return False
        future.set_result(response)
        request_id = self._approval_request_ids.get(tool_call_id)
        if self._approval_registry is not None and request_id:
            self._approval_registry.discard(request_id)
        self._clear_pending_approval(tool_call_id)
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
        if future is not None and not future.done():
            future.set_result(ApprovalResponse(decision="cancelled", feedback=feedback))
            resolved = True
        request_id = self._approval_request_ids.get(tool_call_id)
        if resolve_registry and self._approval_registry is not None and request_id:
            resolved = (
                self._approval_registry.resolve(request_id, "cancelled", feedback)
                or resolved
            )
        elif self._approval_registry is not None and request_id:
            self._approval_registry.discard(request_id)
        if resolved:
            self._clear_pending_approval(tool_call_id)
        return resolved

    async def request_question(
        self,
        ref: ConversationRef,
        request: QuestionRequest,
    ) -> QuestionResponse:
        request = _question_request_with_id(request)
        message = "\n\n".join(_question_message_bubbles(request))
        future: asyncio.Future[QuestionResponse] = asyncio.get_running_loop().create_future()
        self._question_futures[request.tool_call_id] = future
        self._question_refs[request.tool_call_id] = ref
        self._question_request_ids[request.tool_call_id] = request.request_id
        self._question_requests[request.tool_call_id] = request
        registry_future: asyncio.Future[QuestionResponse] | None = None
        if self._question_registry is not None:
            _request_id, registry_future = self._question_registry.register(request)
        try:
            delivery = await self.send_text(ref, message)
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
                        "cancelled by user reply"
                        if decision == "cancelled"
                        else ""
                    ),
                    runtime_missing=True,
                )
            )
            logger.info(
                "追问回复按持久编号记录 | adapter=wechat source=%s request_id=%s",
                turn.ref.source_id or turn.ref.chat_id or "-",
                request_id,
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
            logger.info(
                "追问回复有歧义 | adapter=wechat source=%s pending=%d",
                turn.ref.source_id or turn.ref.chat_id or "-",
                len(tool_call_ids),
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
            logger.info(
                "追问回复已匹配 | adapter=wechat source=%s tool_call_id=%s",
                turn.ref.source_id or turn.ref.chat_id or "-",
                tool_call_id,
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
                logger.info(
                    "审批回复按持久编号记录 | adapter=wechat source=%s request_id=%s decision=%s",
                    turn.ref.source_id or turn.ref.chat_id or "-",
                    reply.target_id,
                    decision,
                )
                return True
            return False
        # /stop during a pending approval cancels it, then forwards /stop downstream.
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
            return self._consume_approval_feedback(turn, tool_call_ids)
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
            logger.info(
                "审批回复有歧义 | adapter=wechat source=%s pending=%d",
                turn.ref.source_id or turn.ref.chat_id or "-",
                len(tool_call_ids),
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
            logger.info(
                "审批回复已匹配 | adapter=wechat source=%s tool_call_id=%s decision=%s",
                turn.ref.source_id or turn.ref.chat_id or "-",
                tool_call_id,
                decision,
            )
        return resolved

    def _consume_approval_feedback(self, turn: InboundTurn, tool_call_ids: list[str]) -> bool:
        """撤销唯一待审批方案，把新文字作为工具拒绝反馈传给同一轮 Agent。

        不直接改参或批准；反馈只记录一次，不再进入串行消息队列。
        命令、带附件的消息和多条待审批场景仍交给原有路由，避免猜测目标或丢失媒体。
        """
        text = str(turn.text or "").strip()
        if len(tool_call_ids) != 1 or not text or text.startswith("/") or turn.attachments:
            return False
        tool_call_id = tool_call_ids[0]
        pending_ref = self._approval_refs[tool_call_id]
        if pending_ref.user_id and turn.ref.user_id and pending_ref.user_id != turn.ref.user_id:
            return False
        request_id = str(self._approval_request_ids.get(tool_call_id) or "")
        feedback = (
            "旧预览未执行。用户在等待确认时发送了新消息，请先处理这条消息；"
            "如果仍需写入，重新提交工具预览并等待明确批准。\n"
            f"用户消息时间（北京时间）：{user_time_iso(turn.timestamp or self.wall_clock_fn())}\n"
            f"用户消息：{text}"
        )
        if not self._resolve_approval_response(
            tool_call_id, ApprovalResponse(decision="deny", feedback=feedback)
        ):
            return False
        self._recorded_inputs.append(
            approval_response_turn(
                turn,
                request_id=request_id,
                tool_call_id=tool_call_id,
                decision="deny",
                feedback=feedback,
            )
        )
        logger.info(
            "审批期间文字反馈已交回 Agent | adapter=wechat source=%s tool_call_id=%s",
            turn.ref.source_id or turn.ref.chat_id or "-",
            tool_call_id,
        )
        return True

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

    def has_pending_approval(self, ref: ConversationRef, *, now: datetime | None = None) -> bool:
        for tool_call_id, future in list(self._approval_futures.items()):
            if future.done():
                continue
            pending_ref = self._approval_refs.get(tool_call_id)
            if pending_ref is not None and _same_approval_ref(pending_ref, ref):
                return True
        if self.state_store is None:
            return False
        return self.state_store.has_pending_approval(
            self.config.account_id,
            ref,
            now=now or self.wall_clock_fn(),
        )

    def _mark_event_seen(self, turn: InboundTurn) -> bool:
        event_id = str(turn.raw_event_id or "").strip()
        if not event_id:
            return True
        if event_id in self._seen_event_ids:
            return False
        self._seen_event_ids.add(event_id)
        self._seen_event_order.append(event_id)
        if len(self._seen_event_order) > 10000:
            expired = self._seen_event_order.popleft()
            self._seen_event_ids.discard(expired)
        return True

    def flush_expired_media(self) -> int:
        now = self.clock_fn()
        flushed = 0
        for key, batch in list(self._pending_media.items()):
            if batch.deadline > now:
                continue
            self._pending_media.pop(key, None)
            self._inbox.append(
                replace(
                    batch.turn,
                    text=DEFAULT_IMAGE_PROMPT,
                    raw_event_id=f"media_flush:{batch.turn.raw_event_id}",
                )
            )
            flushed += 1
        return flushed

    def group_inbox_items(self, items: list[AdapterInboxItem]) -> list[list[AdapterInboxItem]]:
        """后台先持久化图片，领取时合并同一发送者在等待窗口内的后续说明。"""
        now = to_user_time(self.wall_clock_fn())
        wait = timedelta(seconds=max(0, int(self.config.image_followup_wait_seconds)))
        pending: dict[str, list[AdapterInboxItem]] = {}
        ready: list[list[AdapterInboxItem]] = []
        for item in items:
            key = f"{item.account_id}:{_media_buffer_key(item.turn)}"
            received = parse_user_time(item.received_at)
            batch = pending.get(key)
            command = item.turn.text.strip().startswith("/")
            if batch and (received > parse_user_time(batch[-1].received_at) + wait or command):
                ready.append(pending.pop(key))
                batch = None
            if self._should_buffer_media(item.turn):
                pending.setdefault(key, []).append(item)
            elif batch and item.turn.text.strip():
                ready.append([*pending.pop(key), item])
            else:
                if batch:
                    ready.append(pending.pop(key))
                ready.append([item])
        for batch in pending.values():
            if now >= parse_user_time(batch[-1].received_at) + wait:
                ready.append(batch)
        positions = {item.dedupe_key: i for i, item in enumerate(items)}
        return sorted(ready, key=lambda batch: positions[batch[0].dedupe_key])

    def _maybe_merge_pending_media(self, turn: InboundTurn) -> InboundTurn:
        if not str(turn.text or "").strip():
            return turn
        key = _media_buffer_key(turn)
        batch = self._pending_media.pop(key, None)
        if batch is None:
            return turn
        return replace(
            turn,
            attachments=[*batch.turn.attachments, *turn.attachments],
            raw_event_id=turn.raw_event_id or batch.turn.raw_event_id,
        )

    def _should_buffer_media(self, turn: InboundTurn) -> bool:
        if str(turn.text or "").strip():
            return False
        return any(attachment.kind == "image" for attachment in turn.attachments)

    def _buffer_media(self, turn: InboundTurn) -> None:
        key = _media_buffer_key(turn)
        deadline = self.clock_fn() + max(0, int(self.config.image_followup_wait_seconds))
        existing = self._pending_media.get(key)
        if existing is None:
            self._pending_media[key] = PendingMediaBatch(turn=turn, deadline=deadline)
            return
        self._pending_media[key] = PendingMediaBatch(
            turn=replace(
                existing.turn,
                attachments=[*existing.turn.attachments, *turn.attachments],
                raw_event_id=turn.raw_event_id or existing.turn.raw_event_id,
            ),
            deadline=deadline,
        )

    def _remember_context_token(self, raw: dict, turn: InboundTurn) -> None:
        token = str(raw.get("context_token") or "").strip()
        persisted = bool(token and self.state_store is not None and self.config.account_id)
        if persisted:
            self.state_store.set_context_token(self.config.account_id, turn.ref.chat_id, token)
        logger.info("微信入站回复上下文", extra={"event": "wechat.inbound.context",
            "input_event_id": turn.raw_event_id, "recipient_fingerprint": delivery_fingerprint(turn.ref.chat_id),
            "context_fingerprint": delivery_fingerprint(token), "context_persisted": persisted})

    def _context_token_for_ref(self, ref: ConversationRef) -> str:
        if self.state_store is None or not self.config.account_id:
            return ""
        return self.state_store.get_context_token(self.config.account_id, ref.chat_id)

    def _clear_context_token_for_ref(self, ref: ConversationRef) -> None:
        if self.state_store is None or not self.config.account_id:
            return
        self.state_store.clear_context_token(self.config.account_id, ref.chat_id)

    def _record_pending_approval(self, ref: ConversationRef, request: ApprovalRequest) -> None:
        if self.state_store is None:
            return
        now = self.wall_clock_fn()
        expires_at = (
            now + timedelta(seconds=float(request.timeout_seconds))
            if request.timeout_seconds > 0
            else None
        )
        try:
            self.state_store.record_pending_approval(
                self.config.account_id,
                request.tool_call_id,
                ref,
                created_at=now,
                expires_at=expires_at,
            )
        except Exception:
            return

    def _clear_pending_approval(self, tool_call_id: str) -> None:
        if self.state_store is None:
            return
        try:
            self.state_store.clear_pending_approval(self.config.account_id, tool_call_id)
        except Exception:
            return

    async def _resolve_media(self, raw: dict) -> dict:
        if self._media_resolver is None:
            return raw
        resolved = self._media_resolver(raw)
        if hasattr(resolved, "__await__"):
            resolved = await resolved  # type: ignore[assignment]
        return resolved

    async def upload_file(self, ref: ConversationRef, path: Path) -> SendResult:
        del ref
        return SendResult(ok=False, error=f"WeChat upload is not implemented: {path}")


def _config_with_state(config: WeChatConfig, state_store: WeChatStateStore | None) -> WeChatConfig:
    if state_store is None:
        return config
    if config.account_id:
        account = state_store.load_account(config.account_id)
    elif not config.token:
        account = state_store.load_default_account()
    else:
        account = None
    if account is None:
        return config
    return replace(
        config,
        account_id=config.account_id or account.account_id,
        token=config.token or account.token,
        base_url=account.base_url or config.base_url,
    )


def _media_buffer_key(turn: InboundTurn) -> str:
    if turn.is_group:
        return f"group:{turn.ref.chat_id}:{turn.ref.user_id}"
    return turn.ref.source_id or f"dm:{turn.ref.chat_id}"


def _same_approval_ref(left: ConversationRef, right: ConversationRef) -> bool:
    if left.platform != right.platform or left.chat_id != right.chat_id:
        return False
    if left.source_id and right.source_id and left.source_id != right.source_id:
        return False
    if left.thread_id and right.thread_id and left.thread_id != right.thread_id:
        return False
    return True


def _is_stop_command_text(text: object) -> bool:
    command, _, _args = str(text or "").strip().partition(" ")
    return command.lower() == "/stop"


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
    try:
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
    except TimeoutError:
        feedback = f"question timed out after {int(timeout)} seconds"
        return QuestionResponse(decision="timeout", feedback=feedback)


def _question_message_bubbles(request: QuestionRequest) -> list[str]:
    lines = [f"需要你回答\n{request.question}"]
    options = [str(option).strip() for option in list(request.options or []) if str(option).strip()]
    if options:
        lines.append("\n".join(f"{index}. {option}" for index, option in enumerate(options, start=1)))
    if options and not request.allow_free_text:
        lines.append("请回复选项编号或选项文字。")
    elif options:
        lines.append("可回复选项编号、选项文字，或直接输入答案。")
    else:
        lines.append("请直接回复答案。")
    return ["\n".join(part for part in lines if part)]


def _approval_message_bubbles(request: ApprovalRequest) -> list[str]:
    return [_approval_markdown_message(request)]


def _approval_markdown_message(request: ApprovalRequest, *, saved: bool = False) -> str:
    language, body = _approval_code_block(request)
    summary = _approval_summary(request.detail)
    title = request.action if summary else f"{request.tool_name} Approval"
    instruction = "回复【1】撤销；需要修改时直接回复修改内容" if saved else (
        "回复【1】确认，回复【2】取消\n"
        "需要修改时直接回复修改内容\n"
        f"（等待确认：{_approval_timeout_short_label(request.timeout_seconds)}）"
    )
    return (
        f"**{title}**\n"
        f"```{language}\n"
        f"{_code_block_value(body, max_chars=1200)}\n"
        "```\n"
        f"{instruction}"
    )


def _approval_code_block(request: ApprovalRequest) -> tuple[str, str]:
    if request.tool_name == "Bash":
        command = _bash_approval_command(request.detail)
        if command:
            return "bash", command
    return "text", _generic_approval_body(request)


def _generic_approval_body(request: ApprovalRequest) -> str:
    summary = _approval_summary(request.detail)
    if summary:
        return summary
    lines: list[str] = []
    action = str(request.action or "").strip()
    if action:
        lines.append(action)
    if isinstance(request.detail, dict):
        preview = _approval_preview_text(request.detail.get("preview"))
        if preview:
            lines.append(f"预览：{preview}")
        args = request.detail.get("args")
        if isinstance(args, dict):
            rendered_args = _approval_args_text(args)
            if rendered_args:
                lines.append(f"参数：{rendered_args}")
    return "\n".join(lines).strip() or "执行工具调用"


def _approval_summary(detail: object) -> str:
    if isinstance(detail, dict) and isinstance(detail.get("preview"), dict):
        value = detail["preview"].get("summary")
        if isinstance(value, str):
            return value.strip()
    return ""


def _approval_args_text(args: dict) -> str:
    parts: list[str] = []
    for key, value in args.items():
        if value in ("", None, [], {}):
            continue
        rendered = _render_approval_value(value)
        if not rendered:
            continue
        parts.append(f"{key}={rendered}")
        if len(parts) >= 8:
            break
    text = "，".join(parts)
    if len(text) > 500:
        return f"{text[:497]}..."
    return text


def _approval_feedback_for_decision(decision: str) -> str:
    if str(decision or "").strip().lower() == "cancelled":
        return "cancelled by user reply"
    return "denied by user reply"


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


def _bash_approval_command(detail: object | None) -> str:
    if not isinstance(detail, dict):
        return ""
    preview = detail.get("preview")
    if isinstance(preview, dict):
        command = str(preview.get("command") or "").strip()
        if command:
            return command
    args = detail.get("args")
    if isinstance(args, dict):
        return str(args.get("command") or "").strip()
    return ""


def _approval_preview_text(preview: object) -> str:
    if preview in (None, "", [], {}):
        return ""
    if isinstance(preview, str):
        text = preview.strip()
    else:
        text = _render_approval_value(preview)
    if len(text) > 500:
        text = f"{text[:497]}..."
    return text


def _render_approval_value(value: object) -> str:
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, (int, float, bool)):
        text = str(value)
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except TypeError:
            text = str(value)
    if len(text) > 120:
        return f"{text[:117]}..."
    return text


def _code_block_value(value: object, *, max_chars: int) -> str:
    text = str(value or "").strip()
    text = text.replace("```", "`\u200b``")
    if len(text) > max_chars:
        return f"{text[: max_chars - 3]}..."
    return text


def _approval_timeout_short_label(seconds: float) -> str:
    if seconds <= 0:
        return "none"
    if seconds >= 60:
        minutes = max(1, int(round(seconds / 60)))
        return f"{minutes}min"
    whole_seconds = max(1, int(round(seconds)))
    return f"{whole_seconds}s"


def _monotonic() -> float:
    return time.monotonic()
