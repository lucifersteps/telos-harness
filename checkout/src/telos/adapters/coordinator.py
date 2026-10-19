"""Conversation-level turn coordination."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime
import logging
from pathlib import Path

from telos.adapters.approval_channel import AdapterApprovalChannel
from telos.adapters.base import Adapter, InboundTurn, SendResult
from telos.adapters.question_channel import AdapterQuestionChannel, QuestionChannel
from telos.conversation.command_router import (
    ConversationCommandContext,
    ConversationCommandResult,
    ConversationCommandRouter,
    TOOLS_LAST_MODEL_STATE_KEY,
    TOOLS_STATUS_REMINDER_STATE_KEY,
    session_tools_enabled,
)
from telos.adapters.routing import record_last_real_user_route
from telos.adapters.tool_receipts import collect_tool_receipts
from telos.agent.context.system_reminder import (
    SystemReminder,
    SystemReminderContext,
    SystemReminderManager,
)
from telos.agent.hooks.policy.engine import PolicyEngine
from telos.agent.hooks.policy.tool_policy import ToolPolicyHook
from telos.agent.hooks.request import PrepareRequest
from telos.agent.subagents.host import SubagentTurnContext
from telos.agent.tools.manager import ToolManager
from telos.agent.tools.types import ToolResult
from telos.agent.turn_flow import TextTurnResult, run_text_turn
from telos.runtime.cancel import CancelHandle
from telos.runtime.turn_runtime import RunSource, TurnRuntime
from telos.core.clock import user_now
from telos.core.ids import new_turn_id
from telos.core.retry import RetryPolicy
from telos.core.telemetry import NullTelemetrySink, TelemetrySink, emit_telemetry
from telos.memory_space.paths import agent_dir_for_session_dir
from telos.memory_space.runtime import RuntimeStateStore, RuntimeWireEvent, RuntimeWireLedger
from telos.providers.base import ChatProvider
from telos.session.manager import SessionManager
from telos.session.session import Session
from telos.session.state import load_session_state, save_session_state
from telos.tools.builtin.web_sources import (
    WebSearchSourceCache,
    append_web_source_prompt,
    collect_web_search_sources_from_events,
)


ToolManagerFactory = Callable[[Session, SubagentTurnContext], ToolManager]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CoordinatedTurnResult:
    turn: InboundTurn
    response: TextTurnResult
    delivery: SendResult


class TurnCoordinator:
    def __init__(
        self,
        *,
        adapter: Adapter,
        provider: ChatProvider,
        session_manager: SessionManager,
        repo_root: Path,
        tool_manager: ToolManager | None = None,
        tool_manager_factory: ToolManagerFactory | None = None,
        policy_mode: str = "custom",
        tool_approvals: dict[str, bool] | None = None,
        max_steps: int = 20,
        retry_policy: RetryPolicy | None = None,
        command_router: ConversationCommandRouter | None = None,
        web_search_source_cache: WebSearchSourceCache | None = None,
        profile_name: str | None = None,
        profile_path: Path | None = None,
        profile_base_dir: Path | None = None,
        telemetry: TelemetrySink | None = None,
        tool_receipts: object | None = None,
        turn_runtime: TurnRuntime | None = None,
        context_refresh: Callable[[], Awaitable[str]] | None = None,
        prepare_request: PrepareRequest | None = None,
    ):
        self.adapter = adapter
        self.provider = provider
        self.session_manager = session_manager
        self.tool_manager = tool_manager
        self.tool_manager_factory = tool_manager_factory
        self.turn_runtime = turn_runtime
        self.repo_root = repo_root
        self.policy_mode = policy_mode
        self.tool_approvals = dict(tool_approvals or {})
        self.max_steps = max_steps
        self.retry_policy = retry_policy or RetryPolicy()
        self.command_router = command_router
        self.web_search_source_cache = web_search_source_cache
        self.profile_name = profile_name
        self.profile_path = profile_path
        self.profile_base_dir = profile_base_dir
        self.telemetry = telemetry or NullTelemetrySink()
        self.tool_receipts = tool_receipts
        self.context_refresh = context_refresh
        self.prepare_request = prepare_request

    async def handle_turn(
        self,
        turn: InboundTurn,
        abort_check: CancelHandle | None = None,
    ) -> CoordinatedTurnResult:
        session = _session_for_turn(self.session_manager, turn)
        emit_telemetry(
            self.telemetry,
            "turn.received",
            adapter=self.adapter.name,
            session_id=session.id,
            source_id=turn.ref.source_id,
            text_chars=len(turn.text),
            attachments=len(turn.attachments),
        )
        logger.info(
            "开始处理 | adapter=%s session=%s source=%s inbound_messages=%d text_chars=%d attachments=%d",
            self.adapter.name,
            session.id,
            turn.ref.source_id or turn.ref.chat_id or "-",
            _inbound_message_count(turn),
            len(turn.text),
            len(turn.attachments),
        )
        command_result = self._handle_adapter_command(turn, session)
        if command_result is not None:
            return await self._complete_adapter_command_turn(session, turn, command_result)

        approval_channel = AdapterApprovalChannel(adapter=self.adapter, ref=turn.ref)
        question_channel = _question_channel_for_adapter(self.adapter, turn.ref)
        subagent_turn = SubagentTurnContext(
            approval_channel=approval_channel,
            question_channel=question_channel,
            abort_check=abort_check,
            turn_runtime=self.turn_runtime,
            turn_ref=turn.ref,
        )
        tool_manager = self._tool_manager_for_session(session, subagent_turn)

        tools_enabled = session_tools_enabled(session)
        approval_available = _approval_available_for_turn(self.adapter, turn)

        tools = tool_manager.list()
        policy_hook = ToolPolicyHook(
            engine=PolicyEngine(
                mode=self.policy_mode,
                tool_approvals=self.tool_approvals,
                approval_available=approval_available,
                tools_enabled=tools_enabled,
            ),
            approval_channel=approval_channel,
        )
        try:
            if self.context_refresh is not None:
                content = await self.context_refresh()
                turn = replace(turn, metadata={
                    **turn.metadata,
                    "system_reminders": [
                        *list(turn.metadata.get("system_reminders") or []),
                        {"source": "external_state", "content": content},
                    ],
                })
            recovery_continuation = _recovery_continuation_options(turn)
            result = await run_text_turn(
                session=session,
                provider=self.provider,
                user_text=turn.text,
                repo_root=self.repo_root,
                data_dir=self.session_manager.paths.agent_dir,
                profile_name=self.profile_name,
                profile_path=self.profile_path,
                profile_base_dir=self.profile_base_dir,
                attachments=list(turn.attachments),
                tools=tools,
                policy_hook=policy_hook,
                system_reminder_manager=_message_system_reminder_manager(
                    session,
                    turn=turn,
                ),
                message_from=str(turn.metadata.get("message_from") or ""),
                message_time=turn.timestamp or _adapter_wall_clock(self.adapter),
                max_steps=self.max_steps,
                retry_policy=self.retry_policy,
                prepare_request=self.prepare_request,
                abort_check=abort_check,
                turn_runtime=self.turn_runtime,
                run_source=_run_source_for_turn(turn),
                input_message_id=str(
                    turn.metadata.get("conversation_message_id") or ""
                ),
                input_kind=str(turn.metadata.get("conversation_input_kind") or ""),
                exclude_context_turn_ids=[
                    str(item)
                    for item in list(turn.metadata.get("exclude_context_turn_ids") or [])
                    if str(item or "").strip()
                ],
                **recovery_continuation,
            )
        except Exception as error:
            logger.exception(
                "处理失败 | adapter=%s session=%s error_type=%s",
                self.adapter.name,
                session.id,
                type(error).__name__,
            )
            emit_telemetry(
                self.telemetry,
                "turn.failed",
                adapter=self.adapter.name,
                session_id=session.id,
                error_type=type(error).__name__,
                error=str(error),
            )
            raise
        _record_last_real_user_route_for_turn(
            session=session,
            turn=turn,
            adapter=self.adapter,
            turn_id=result.turn_id,
        )
        _record_tools_status_seen_by_model(session, tools_enabled)
        if result.aborted:
            emit_telemetry(
                self.telemetry,
                "turn.completed",
                adapter=self.adapter.name,
                session_id=session.id,
                turn_id=result.turn_id,
                delivery_ok=True,
                response_chars=0,
                aborted=True,
            )
            logger.info(
                "处理已中止 | adapter=%s session=%s turn=%s",
                self.adapter.name,
                session.id,
                result.turn_id,
            )
            return CoordinatedTurnResult(
                turn=turn,
                response=result,
                delivery=SendResult(ok=True),
            )
        result = self._apply_post_turn_features(session, turn, result)
        await self._send_tool_receipts(
            session=session,
            turn=turn,
            turn_id=result.turn_id,
            events=result.events,
        )
        if _should_suppress_scheduled_delivery(turn, result.response):
            delivery = SendResult(ok=True)
        else:
            try:
                delivery = await self.adapter.send_text(turn.ref, result.response)
            except Exception as error:
                logger.exception(
                    "回复发送异常 | adapter=%s session=%s turn=%s",
                    self.adapter.name,
                    session.id,
                    result.turn_id,
                )
                _append_delivery_record(
                    session=session,
                    turn_id=result.turn_id,
                    adapter_name=self.adapter.name,
                    turn=turn,
                    text=result.response,
                    delivery=SendResult(ok=False, error=str(error)),
                    exception=error,
                )
                emit_telemetry(
                    self.telemetry,
                    "turn.failed",
                    adapter=self.adapter.name,
                    session_id=session.id,
                    turn_id=result.turn_id,
                    error_type=type(error).__name__,
                    error=str(error),
                )
                raise
        _append_delivery_record(
            session=session,
            turn_id=result.turn_id,
            adapter_name=self.adapter.name,
            turn=turn,
            text=result.response,
            delivery=delivery,
        )
        emit_telemetry(
            self.telemetry,
            "turn.completed",
            adapter=self.adapter.name,
            session_id=session.id,
            turn_id=result.turn_id,
            delivery_ok=delivery.ok,
            response_chars=len(result.response),
        )
        logger.info(
            "处理完成 | adapter=%s session=%s turn=%s delivery=%s response_chars=%d",
            self.adapter.name,
            session.id,
            result.turn_id,
            _delivery_status(delivery),
            len(result.response),
        )
        return CoordinatedTurnResult(turn=turn, response=result, delivery=delivery)

    async def _complete_adapter_command_turn(
        self,
        session: Session,
        turn: InboundTurn,
        command_result: ConversationCommandResult,
    ) -> CoordinatedTurnResult:
        result = _record_command_turn(
            session=session,
            adapter_name=self.adapter.name,
            turn=turn,
            command_result=command_result,
        )
        _record_last_real_user_route_for_turn(
            session=session,
            turn=turn,
            adapter=self.adapter,
            turn_id=result.turn_id,
            route_session_id=_route_session_id_for_command(session, command_result),
        )
        delivery = SendResult(ok=True)
        try:
            if result.response:
                delivery = await self.adapter.send_text(turn.ref, result.response)
        except Exception as error:
            logger.exception(
                "回复发送异常 | adapter=%s session=%s turn=%s",
                self.adapter.name,
                session.id,
                result.turn_id,
            )
            _append_delivery_record(
                session=session,
                turn_id=result.turn_id,
                adapter_name=self.adapter.name,
                turn=turn,
                text=result.response,
                delivery=SendResult(ok=False, error=str(error)),
                exception=error,
            )
            emit_telemetry(
                self.telemetry,
                "turn.failed",
                adapter=self.adapter.name,
                session_id=session.id,
                turn_id=result.turn_id,
                error_type=type(error).__name__,
                error=str(error),
            )
            raise
        _append_delivery_record(
            session=session,
            turn_id=result.turn_id,
            adapter_name=self.adapter.name,
            turn=turn,
            text=result.response,
            delivery=delivery,
        )
        emit_telemetry(
            self.telemetry,
            "turn.completed",
            adapter=self.adapter.name,
            session_id=session.id,
            turn_id=result.turn_id,
            delivery_ok=delivery.ok,
            response_chars=len(result.response),
            command=command_result.command,
        )
        logger.info(
            "处理完成 | adapter=%s session=%s turn=%s command=%s delivery=%s response_chars=%d",
            self.adapter.name,
            session.id,
            result.turn_id,
            command_result.command,
            _delivery_status(delivery),
            len(result.response),
        )
        self._record_command_run_events(
            session=session, turn=turn, result=result, command_result=command_result
        )
        return CoordinatedTurnResult(turn=turn, response=result, delivery=delivery)

    def _record_command_run_events(
        self,
        *,
        session: Session,
        turn: InboundTurn,
        result: TextTurnResult,
        command_result: ConversationCommandResult,
    ) -> None:
        """把命令 turn 也写进 run_events，让 web 端能像普通 turn 一样通过会话事件日志
        （canonical conversation.event 通道）转发它：输入气泡 + 命令回复 + 完成。否则命令 turn
        只落 wire.jsonl，不进 run_events，web 端提交后只收到 turn_queued、永远等不到
        turn_started/turn_completed → 界面卡死。

        全同步写入（无 await），对同一事件循环里的 watcher 而言是原子的，不会被观察到半成品。"""

        if self.turn_runtime is None:
            return
        run_events = self.turn_runtime.run_events
        turn_id = result.turn_id
        try:
            run_events.start_turn_run(
                turn_id=turn_id,
                session_id=session.id,
                agent_id=session.metadata.root_agent_id,
                source_platform=str(turn.ref.platform or ""),
                source_id=str(turn.ref.source_id or ""),
                chat_id=str(turn.ref.chat_id or ""),
                user_id=str(turn.ref.user_id or ""),
            )
            durable: list[tuple[str, dict[str, object]]] = [
                (
                    "inbound.message.received",
                    {
                        "text_preview": str(turn.text or "")[:4000],
                        "text_chars": len(str(turn.text or "")),
                        "origin": str(turn.ref.platform or ""),
                        "input_kind": "registered_command",
                        "command_name": str(
                            (turn.metadata or {}).get("conversation_command_name") or ""
                        ),
                        "command_token": str(
                            (turn.metadata or {}).get("conversation_command_token")
                            or f"/{command_result.command}"
                        ),
                    },
                ),
            ]
            if str(result.response or ""):
                durable.append(
                    (
                        "assistant.message.completed",
                        {
                            "content": result.response,
                            "input_kind": "registered_command",
                            "command_name": command_result.command,
                            "command_token": str(
                                (turn.metadata or {}).get("conversation_command_token")
                                or f"/{command_result.command}"
                            ),
                        },
                    )
                )
            durable.append(("turn.completed", {"steps": 0, "command": command_result.command}))
            for seq, (event_type, payload) in enumerate(durable, start=1):
                run_events.append_event(
                    turn_id=turn_id, seq=seq, type=event_type, payload=payload
                )
            run_events.finish_turn_run(
                turn_id, status="completed", stop_reason="adapter_command"
            )
        except Exception:  # noqa: BLE001 - 记录失败不该拖垮命令本身
            logger.debug("记录命令 turn run_events 失败 | turn=%s", turn_id, exc_info=True)

    async def _send_tool_receipts(
        self,
        *,
        session: Session,
        turn: InboundTurn,
        turn_id: str,
        events: list[object],
    ) -> None:
        receipts = collect_tool_receipts(
            events,
            self.tool_receipts,
            adapter_name=self.adapter.name,
            platform=turn.ref.platform,
        )
        for receipt in receipts:
            try:
                delivery = await self.adapter.send_text(turn.ref, receipt.text)
            except Exception as error:
                logger.exception(
                    "工具回执发送异常 | adapter=%s session=%s turn=%s tool=%s",
                    self.adapter.name,
                    session.id,
                    turn_id,
                    receipt.tool_name,
                )
                _append_delivery_record(
                    session=session,
                    turn_id=turn_id,
                    adapter_name=self.adapter.name,
                    turn=turn,
                    text=receipt.text,
                    delivery=SendResult(ok=False, error=str(error)),
                    exception=error,
                )
                continue
            _append_delivery_record(
                session=session,
                turn_id=turn_id,
                adapter_name=self.adapter.name,
                turn=turn,
                text=receipt.text,
                delivery=delivery,
            )

    def _apply_post_turn_features(
        self,
        session: Session,
        turn: InboundTurn,
        result: TextTurnResult,
    ) -> TextTurnResult:
        if self.web_search_source_cache is None:
            return result
        used_web_search, sources = collect_web_search_sources_from_events(result.events)
        if not used_web_search:
            return result
        if sources:
            self.web_search_source_cache.set_for_turn(session, turn, sources)
            return replace(result, response=append_web_source_prompt(result.response))
        self.web_search_source_cache.clear_for_turn(session, turn)
        return result

    def _tool_manager_for_session(
        self,
        session: Session,
        subagent_turn: SubagentTurnContext,
    ) -> ToolManager:
        if self.tool_manager_factory is not None:
            return self.tool_manager_factory(session, subagent_turn)
        if self.tool_manager is not None:
            return self.tool_manager
        return ToolManager()

    def _handle_adapter_command(
        self,
        turn: InboundTurn,
        session: Session,
    ) -> ConversationCommandResult | None:
        if self.command_router is None:
            return None
        context = ConversationCommandContext(session_manager=self.session_manager)
        return self.command_router.route(turn, session, context)

def _adapter_wall_clock(adapter: Adapter) -> datetime:
    clock = getattr(adapter, "wall_clock_fn", None)
    if callable(clock):
        try:
            value = clock()
        except Exception:
            value = None
        if isinstance(value, datetime):
            return value
    return user_now()


def _run_source_for_turn(turn: InboundTurn) -> RunSource:
    ref = turn.ref
    return RunSource(
        platform=ref.platform,
        source_id=ref.source_id,
        chat_id=ref.chat_id,
        thread_id=ref.thread_id,
        user_id=ref.user_id,
        raw_event_id=str(turn.raw_event_id or ""),
    )


def _approval_available_for_turn(adapter: Adapter, turn: InboundTurn) -> bool:
    if _scheduler_approval_adapter(adapter) is not None:
        return True
    text = str(turn.text or "").lstrip().lower()
    return not (
        (turn.ref.platform or "").strip().lower() == "scheduler"
        or (adapter.name or "").strip().lower() == "scheduler"
        or text.startswith("<schedulefire")
    )


def _scheduler_approval_adapter(adapter: Adapter) -> Adapter | None:
    inbound = getattr(adapter, "inbound", adapter)
    if str(getattr(inbound, "name", "") or "").strip().lower() == "scheduler":
        return inbound
    return None


def _question_channel_for_adapter(adapter: Adapter, ref: object) -> QuestionChannel | None:
    adapter_for = getattr(adapter, "_adapter_for", None)
    target = adapter_for(ref) if callable(adapter_for) else adapter
    if not callable(getattr(target, "request_question", None)):
        return None
    return AdapterQuestionChannel(adapter=adapter, ref=ref)  # type: ignore[arg-type]


def _recovery_continuation_options(turn: InboundTurn) -> dict[str, object]:
    metadata = dict(turn.metadata or {})
    kind = str(metadata.get("recovery_continuation_kind") or "").strip()
    if kind not in {"approval", "question"}:
        return {}
    tool_call_id = str(metadata.get("recovery_tool_call_id") or "").strip()
    tool_name = str(metadata.get("recovery_tool_name") or "").strip()
    raw_args = metadata.get("recovery_tool_args")
    tool_args = dict(raw_args) if isinstance(raw_args, dict) else {}
    if not tool_call_id or not tool_name:
        return {}
    options: dict[str, object] = {
        "suppress_inbound_message_event": True,
        "resume_tool_call": {
            "id": tool_call_id,
            "name": tool_name,
            "arguments": tool_args,
        },
        "resume_step": int(metadata.get("recovery_tool_step") or 1),
        "resume_tool_event_turn_id": str(
            metadata.get("retry_after_turn_id") or ""
        ),
        "resume_tool_previous_status": str(
            metadata.get("recovery_tool_status") or ""
        ),
    }
    decision = str(metadata.get("recovery_decision") or "").strip().lower()
    if kind == "approval":
        if decision in {"approve", "approved", "yes", "1"}:
            raw_response = metadata.get("recovery_response")
            response = dict(raw_response) if isinstance(raw_response, dict) else {}
            modified_args = response.get("modified_args")
            if isinstance(modified_args, dict):
                options["resume_tool_call"] = {
                    "id": tool_call_id,
                    "name": tool_name,
                    "arguments": dict(modified_args),
                }
            options["resume_tool_approved"] = True
            return options
        feedback = str(metadata.get("recovery_feedback") or decision or "denied")
        options["resume_tool_result"] = ToolResult(
            output={"status": "denied", "feedback": feedback},
            is_error=True,
            error="tool_approval_denied",
        )
        return options
    raw_response = metadata.get("recovery_response")
    response = dict(raw_response) if isinstance(raw_response, dict) else {}
    answer = str(response.get("answer") or metadata.get("recovery_answer") or "")
    selected_options = response.get("selected_options")
    selected = (
        [str(item) for item in selected_options]
        if isinstance(selected_options, list)
        else []
    )
    if decision in {"answered", "answer", "resolved", ""}:
        options["resume_tool_result"] = ToolResult(
            output={
                "status": "answered",
                "answer": answer,
                "selected_options": selected,
            }
        )
        return options
    feedback = str(response.get("feedback") or metadata.get("recovery_feedback") or "")
    status = decision or "cancelled"
    options["resume_tool_result"] = ToolResult(
        output={
            "status": status,
            "answer": answer,
            "selected_options": selected,
            "feedback": feedback,
        },
        is_error=True,
        error=f"user_question_{status}",
        retryable=status in {"timeout", "cancelled", "unavailable"},
    )
    return options


def _should_suppress_scheduled_delivery(turn: InboundTurn, response: str) -> bool:
    text = str(turn.text or "").lstrip().lower()
    return (
        text.startswith("<schedulefire")
        and str(response or "").strip().startswith("[SILENT]")
    )


def _inbound_message_count(turn: InboundTurn) -> int:
    return max(1, len(turn.metadata.get("adapter_inbox_keys", [])))


def _delivery_status(delivery: SendResult) -> str:
    if delivery.ok:
        return "ok"
    error = str(delivery.error or "").strip()
    return f"failed:{error}" if error else "failed"


def _record_command_turn(
    *,
    session: Session,
    adapter_name: str,
    turn: InboundTurn,
    command_result: ConversationCommandResult,
) -> TextTurnResult:
    session_dir = Path(session.directory)
    runtime_ledger = RuntimeWireLedger.for_agent_dir(agent_dir_for_session_dir(session_dir))
    turn_id = new_turn_id()
    agent_id = session.metadata.root_agent_id
    runtime_ledger.append(
        RuntimeWireEvent.create(
            type="adapter.command",
            session_id=session.id,
            agent_id=agent_id,
            turn_id=turn_id,
            data={
                "adapter": adapter_name,
                "conversation_ref": asdict(turn.ref),
                "command": command_result.command,
                "text_chars": len(turn.text),
                "attachments": len(turn.attachments),
                "response_chars": len(command_result.response),
                "data": dict(command_result.data),
            },
        )
    )
    runtime_ledger.append(
        RuntimeWireEvent.create(
            type="turn.end",
            session_id=session.id,
            agent_id=agent_id,
            turn_id=turn_id,
            data={
                "stop_reason": "adapter_command",
                "steps": 0,
                "command": command_result.command,
            },
        )
    )
    state = load_session_state(session_dir).touch_message()
    save_session_state(session_dir, state)
    return TextTurnResult(
        response=command_result.response,
        turn_id=turn_id,
        runtime_wire_path=runtime_ledger.path,
    )


class _MetadataReminderProvider:
    def __init__(self, turn: InboundTurn):
        self.turn = turn

    def collect(self, context: SystemReminderContext) -> list[SystemReminder]:
        del context
        reminders = self.turn.metadata.get("system_reminders")
        if not isinstance(reminders, list):
            return []
        result: list[SystemReminder] = []
        for item in reminders:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            result.append(
                SystemReminder(
                    content=content,
                    source=str(item.get("source") or "event"),
                )
            )
        return result


class _ToolsStatusReminderProvider:
    def __init__(self, session: Session):
        self.session = session

    def collect(self, context) -> list[SystemReminder]:
        del context
        session_dir = Path(self.session.directory)
        state = load_session_state(session_dir)
        tools_enabled = session_tools_enabled(self.session)
        last_model_enabled = _tools_notice_enabled(
            state.values.get(TOOLS_LAST_MODEL_STATE_KEY)
        )
        if last_model_enabled is None:
            last_model_enabled = True
        if tools_enabled != last_model_enabled:
            return [_tools_status_reminder(tools_enabled, changed=True)]
        if not tools_enabled:
            return [_tools_status_reminder(False, changed=False)]
        return []


def _message_system_reminder_manager(
    session: Session,
    *,
    turn: InboundTurn,
) -> SystemReminderManager:
    return SystemReminderManager(
        [
            _MetadataReminderProvider(turn),
            _ToolsStatusReminderProvider(session),
        ]
    )


def _tools_notice_enabled(notice: object) -> bool | None:
    if isinstance(notice, dict) and "tools_enabled" in notice:
        return bool(notice.get("tools_enabled"))
    if isinstance(notice, bool):
        return bool(notice)
    return None


def _tools_status_reminder(tools_enabled: bool, *, changed: bool) -> SystemReminder:
    if tools_enabled:
        content = (
            "工具调用已开启。本轮可以在确有需要时调用工具；仍需遵守工具策略和确认流程。"
            if changed
            else "工具调用当前处于开启状态。"
        )
    else:
        content = (
            "工具调用已关闭。禁止调用任何工具；如果需要外部信息或执行操作，请直接说明工具调用当前关闭。"
            if changed
            else "工具调用当前处于关闭状态。禁止调用任何工具；如果需要外部信息或执行操作，请直接说明工具调用当前关闭。"
        )
    return SystemReminder(content=content, source="tool_status")


def _record_tools_status_seen_by_model(session: Session, tools_enabled: bool) -> None:
    session_dir = Path(session.directory)
    state = load_session_state(session_dir)
    updated = state.with_value(
        TOOLS_LAST_MODEL_STATE_KEY,
        {"tools_enabled": bool(tools_enabled)},
    ).without_value(TOOLS_STATUS_REMINDER_STATE_KEY)
    save_session_state(session_dir, updated)


def _record_last_real_user_route_for_turn(
    *,
    session: Session,
    turn: InboundTurn,
    adapter: Adapter,
    turn_id: str,
    route_session_id: str = "",
) -> None:
    if not _is_real_user_turn(adapter, turn) or _is_temporary_session(session):
        return
    record_last_real_user_route(
        RuntimeStateStore.for_agent_dir(agent_dir_for_session_dir(Path(session.directory))),
        ref=turn.ref,
        session_id=route_session_id or session.id,
        turn_id=turn_id,
    )


def _session_for_turn(manager: SessionManager, turn: InboundTurn) -> Session:
    """优先使用会话控制层写入的明确会话编号。"""

    session_id = str((turn.metadata or {}).get("conversation_session_id") or "").strip()
    if not session_id:
        return manager.get_or_create(turn.ref)
    session = manager.load(session_id)
    if session is None:
        raise RuntimeError(f"明确指定的会话不存在：{session_id}")
    return session


def _is_temporary_session(session: Session) -> bool:
    root = session.metadata.agents.get(session.metadata.root_agent_id)
    return str(getattr(root, "profile_name", "") or "").strip() == "temporary"


def _is_real_user_turn(adapter: Adapter, turn: InboundTurn) -> bool:
    if bool(turn.metadata.get("synthetic")):
        return False
    if str(adapter.name or "").strip().lower() == "scheduler":
        return False
    if str(turn.ref.platform or "").strip().lower() == "scheduler":
        return False
    return True


def _route_session_id_for_command(
    session: Session,
    command_result: ConversationCommandResult,
) -> str:
    data = dict(command_result.data)
    for key in ("session_id", "new_session_id", "loaded_session_id"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return session.id


def _append_delivery_record(
    *,
    session: Session,
    turn_id: str,
    adapter_name: str,
    turn: InboundTurn,
    text: str,
    delivery: SendResult,
    exception: Exception | None = None,
) -> None:
    data = {
        "adapter": adapter_name,
        "conversation_ref": asdict(turn.ref),
        "ok": bool(delivery.ok),
        "message_id": delivery.message_id,
        "message_count": int(delivery.message_count or 0),
        "error": delivery.error,
        "text_chars": len(text),
    }
    if exception is not None:
        data["exception_type"] = type(exception).__name__
    RuntimeWireLedger.for_agent_dir(
        agent_dir_for_session_dir(Path(session.directory))
    ).append(
        RuntimeWireEvent.create(
            type="adapter.delivery",
            session_id=session.id,
            agent_id=session.metadata.root_agent_id,
            turn_id=turn_id,
            data=data,
        )
    )
