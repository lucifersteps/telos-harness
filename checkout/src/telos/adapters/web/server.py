"""FastAPI 应用：/ws 聊天 + 只读 REST + 静态 SPA。不碰 sqlite，只调 view/service。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from telos.activity.assign import retry_failed_assign
from telos.adapters.base import ConversationRef, InboundTurn
from telos.adapters.coordinator import TurnCoordinator
from telos.adapters.question_registry import (
    PendingQuestionRegistry,
    RegistryQuestionChannel,
)
from telos.adapters.question_reply import cancel_pending_questions_by_stop
from telos.adapters.web._snapshot import (
    _approval_snapshot,
    _client_event_ack_snapshot,
    _conversation_event_snapshot,
    _conversation_events_page,
    _dedupe_strings,
    _input_command_definition_snapshot,
    _input_request_snapshot,
    _latest_draft_snapshot,
    _latest_recovery_snapshot,
    _message_snapshot,
    _queue_snapshot,
    _runtime_cancel_tree_snapshot,
    _session_actions,
    _tool_call_snapshot,
    _turn_run_snapshot,
)
from telos.adapters.web.adapter import WebAdapter
from telos.adapters.web.approval_registry import (
    PendingApprovalRegistry,
    RegistryApprovalChannel,
)
from telos.adapters.web.gui_static import GUI_APP_DIR
from telos.adapters.web.gui_static import gui_app_response as _gui_app_response
from telos.adapters.web.permissions import (
    PERMISSION_KINDS,
    permissions_status,
    request_permission,
)
from telos.adapters.web.turn_streaming import (
    TurnStreamControl,
    active_turn_id,
    forward_live_deltas,
    run_send_path_turn,
    websocket_server_event,
)
from telos.adapters.web.wechat_login import WeChatLoginSession
from telos.agent.session_snapshot import session_tool_schema_names
from telos.assistants.registry import AssistantRegistry, validate_assistant_id
from telos.conversation.command_events import record_conversation_command_result
from telos.conversation.command_scheduler import (
    ConversationCommandScheduler,
    SharedCommandSchedulerHandle,
)
from telos.conversation.commands import command_blocked_reason, default_command_registry
from telos.conversation.controller import (
    ConversationController,
    ConversationSubmitResult,
    append_stop_result_event,
    update_conversation_turn_status,
)
from telos.conversation.scheduler import (
    ConversationSessionScheduler,
    SharedConversationSchedulerHandle,
)
from telos.conversation.types import (
    CancelTarget,
    CommandAvailability,
    ConversationAction,
    ConversationAppendMode,
    ConversationApprovalStatus,
    ConversationInput,
    ConversationInputKind,
    ConversationInputRequestKind,
    ConversationInputRequestStatus,
    ConversationMessageStatus,
    ConversationToolCallStatus,
)
from telos.core.build_info import build_info
from telos.core.coerce import safe_int
from telos.core.logging import get_logger
from telos.core.settings import load_config_mapping, load_settings, write_config_mapping
from telos.memory_space.context import ContextMessageRepository
from telos.memory_space.recap import RecapRepository, recaps_covering_turn_ids
from telos.memory_space.runtime import RuntimeStateStore
from telos.runtime.cancel import CancelHandle
from telos.runtime.supervisor import (
    AgentRuntimeSupervisor,
    request_runtime_control,
    snapshot_from_control_response,
)
from telos.session.manager import SessionManager
from telos.storage.locks import file_lock
from telos.views import activity_view
from telos.views.chat_history_view import ChatHistoryView
from telos.views.runtime_view import RuntimeView
from telos.views.settings_ui import (
    local_apps_payload,
    set_tool_approval,
    settings_restart_required,
    settings_ui_payload,
    update_settings_from_ui,
)

_HISTORY_FILTERS = {"all", "failed", "tools", "approvals"}

_WEBSOCKET_QUEUE_ACTIONS = frozenset(
    {
        "cancel_message",
        "edit_message",
        "set_message_append_mode",
        "promote_message",
        "reorder_messages",
        "resume_message",
        "retry_message",
        "retry_tool_call",
        "edit_last_message",
    }
)


def _action_target_id(data: dict[str, Any] | None) -> str:
    payload = data or {}
    for key in (
        "message_id",
        "tool_record_id",
        "tool_call_id",
        "approval_id",
        "request_id",
        "turn_id",
        "agent_id",
        "task_name",
    ):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _history_filter_param(value: str) -> str:
    clean = str(value or "all").strip().lower()
    return clean if clean in _HISTORY_FILTERS else "all"


def _action_error_detail(detail: Any) -> tuple[str, Any]:
    if isinstance(detail, dict):
        message = str(detail.get("message") or detail.get("detail") or "").strip()
        if not message:
            try:
                message = json.dumps(detail, ensure_ascii=False, default=str)
            except TypeError:
                message = str(detail)
        return message, detail
    message = str(detail or "").strip()
    return message, detail


def _action_result_payload(
    *,
    ok: bool,
    action: str,
    client_action_id: str = "",
    target_id: str = "",
    reason: str = "",
    code: str = "",
    retryable: bool = False,
    detail: Any = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": bool(ok),
        "action": str(action or ""),
        "client_action_id": str(client_action_id or ""),
        "target_id": str(target_id or ""),
        "reason": str(reason or ""),
        "retryable": bool(retryable),
    }
    if code:
        payload["code"] = str(code)
    if detail is not None:
        payload["detail"] = detail
    return payload


def _http_action_response(
    result: dict[str, Any],
    *,
    action: str,
    target_id: str = "",
) -> dict[str, Any]:
    payload = dict(result)
    payload["action_result"] = _action_result_payload(
        ok=bool(payload.get("ok", True)),
        action=action,
        target_id=target_id,
        reason="completed",
    )
    return payload


def _action_success_event(
    *,
    surface: str,
    action: str,
    client_action_id: str = "",
    target_id: str = "",
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return websocket_server_event(
        "queue.action.completed",
        surface=surface,
        action=action,
        client_action_id=client_action_id,
        result=result or {"ok": True},
        action_result=_action_result_payload(
            ok=True,
            action=action,
            client_action_id=client_action_id,
            target_id=target_id,
        ),
    )


def _action_error_event(
    *,
    action: str,
    client_action_id: str = "",
    target_id: str = "",
    error: HTTPException,
) -> dict[str, Any]:
    message, detail = _action_error_detail(error.detail)
    if not message:
        message = "动作处理失败。"
    return websocket_server_event(
        "ui.error",
        message=message,
        action=action,
        client_action_id=client_action_id,
        action_result=_action_result_payload(
            ok=False,
            action=action,
            client_action_id=client_action_id,
            target_id=target_id,
            reason=message,
            code=f"http_{int(error.status_code)}",
            retryable=int(error.status_code) >= 500,
            detail=detail,
        ),
    )


def _action_message_event(
    event_type: str,
    *,
    action: str,
    message: str,
    ok: bool,
    client_action_id: str = "",
    target_id: str = "",
    reason: str = "",
    code: str = "",
    retryable: bool = False,
    detail: Any = None,
) -> dict[str, Any]:
    return websocket_server_event(
        event_type,
        message=message,
        action=action,
        client_action_id=client_action_id,
        action_result=_action_result_payload(
            ok=ok,
            action=action,
            client_action_id=client_action_id,
            target_id=target_id,
            reason=reason or message,
            code=code,
            retryable=retryable,
            detail=detail,
        ),
    )


def _stop_action_event(
    *,
    surface: str,
    action: str,
    client_action_id: str = "",
    target_id: str = "",
    cancelled: bool,
    turn_id: str = "",
    cancel_tree: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean_target_id = str(target_id or "").strip()
    clean_turn_id = str(turn_id or "").strip()
    if clean_turn_id.startswith("session:"):
        clean_turn_id = ""
    result = {
        "ok": bool(cancelled),
        "cancelled": bool(cancelled),
        "target_id": clean_target_id,
        "turn_id": clean_turn_id,
    }
    if cancel_tree:
        result["cancel_tree"] = dict(cancel_tree)
    if cancelled:
        return _action_success_event(
            surface=surface,
            action=action,
            client_action_id=client_action_id,
            target_id=clean_target_id,
            result=result,
        )
    return _action_message_event(
        "ui.notice",
        action=action,
        client_action_id=client_action_id,
        target_id=clean_target_id,
        message="没有正在运行的任务。",
        ok=False,
        reason="no_running_task",
        code="no_running_task",
        detail=result,
    )


@dataclass
class WebDeps:
    session_manager: SessionManager
    coordinator: TurnCoordinator
    web_adapter: WebAdapter
    runtime_view: RuntimeView
    chat_history_view: ChatHistoryView
    main_ref: ConversationRef
    provider_name: str
    conversation_controller: ConversationController
    # 注册命令暂时仍走命令执行通道；普通消息走持久会话调度器。
    primary_command_handle: SharedCommandSchedulerHandle | None = None
    primary_scheduler_handle: SharedConversationSchedulerHandle | None = None
    background_scheduler_handle: Any | None = None
    local_command_scheduler: ConversationCommandScheduler | None = None
    local_message_scheduler: ConversationSessionScheduler | None = None
    assistant_message_scheduler: ConversationSessionScheduler | None = None
    assistant_service: Any | None = None
    approval_registry: PendingApprovalRegistry | None = None
    approval_channel: RegistryApprovalChannel | None = None
    question_registry: PendingQuestionRegistry | None = None
    question_channel: RegistryQuestionChannel | None = None
    timeline_view: Any | None = None
    overview_view: Any | None = None
    screenshot_root: Path | None = None
    recap_view: Any | None = None
    log_view: Any | None = None
    things_view: Any | None = None
    settings: Any | None = None
    runtime_supervisor: Any | None = None
    request_runtime_restart: Callable[[], None] | None = None


def _main_final(result: Any) -> tuple[str, str, bool]:
    response = result.response
    return response.turn_id, response.response, bool(response.aborted)


def _assistant_final(result: Any) -> tuple[str, str, bool]:
    return result.turn_id, result.response, str(result.status) == "aborted"


def create_app(deps: WebDeps) -> FastAPI:
    app = FastAPI(title="Telos Web Console", docs_url=None, redoc_url=None)

    @app.exception_handler(Exception)
    async def _api_error_handler(request: Request, exc: Exception):  # noqa: ANN202
        # 本地单用户控制台：把未捕获异常的真实原因作为 detail 返回给前端（而非笼统 500），
        # 供各视图展示失败原因 + 重试。HTTPException 仍走 FastAPI 默认（已带 detail）。
        get_logger("telos.apps.web").exception("API 处理失败 | path=%s", request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": f"{type(exc).__name__}: {exc}"},
        )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        surfaces = ["main", *_assistant_ids(deps)]
        readiness = {
            surface: _message_scheduler(deps, surface) is not None
            for surface in surfaces
        }
        return {
            "ok": True,
            "provider": deps.provider_name,
            "surfaces": surfaces,
            "readiness": readiness,
            "ready": all(readiness.values()),
        }

    @app.get("/api/app/build")
    def app_build() -> dict[str, Any]:
        info = build_info()
        return {
            "version": info.version,
            "build_id": info.build_id,
            "commit": info.commit,
            "built_at": info.built_at,
        }

    @app.get("/api/runtime/status")
    def runtime_status() -> dict[str, Any]:
        supervisor = _runtime_supervisor(deps)
        response = request_runtime_control(supervisor.control_socket_path, {"action": "status"})
        snapshot = snapshot_from_control_response(response) or supervisor.snapshot()
        surfaces = ["main", *_assistant_ids(deps)]
        return {
            "ok": True,
            "runtime": snapshot.to_dict(),
            "control": bool(response),
            "readiness": {
                surface: _message_scheduler(deps, surface) is not None
                for surface in surfaces
            },
        }

    @app.post("/api/runtime/stop")
    def runtime_stop() -> dict[str, Any]:
        return _runtime_control_response(
            deps,
            {"action": "stop-runtime", "terminate_self": True},
        )

    @app.post("/api/runtime/restart")
    def runtime_restart() -> dict[str, Any]:
        if deps.request_runtime_restart is None:
            raise HTTPException(status_code=503, detail="当前启动方式不支持重启服务")
        deps.request_runtime_restart()
        return {"ok": True, "action": "restart-runtime"}

    @app.post("/api/runtime/services/{service}/stop")
    def runtime_stop_service(service: str) -> dict[str, Any]:
        return _runtime_control_response(deps, {"action": "stop-service", "service": service})

    @app.post("/api/runtime/services/{service}/restart")
    def runtime_restart_service(service: str) -> dict[str, Any]:
        return _runtime_control_response(deps, {"action": "restart-service", "service": service})

    @app.get("/api/overview")
    def overview() -> dict[str, Any]:
        if deps.overview_view is None:
            raise HTTPException(status_code=503, detail="概览暂不可用")
        return asdict(deps.overview_view.snapshot())

    @app.get("/api/main/history")
    def main_history(
        before_seq: int = 0, limit: int = 80, filter: str = "all"
    ) -> dict[str, Any]:
        return _history_response(
            deps, "main", before_seq=before_seq, limit=limit, history_filter=filter
        )

    @app.get("/api/main/previous-history")
    def main_previous_history(before_seq: int = 0, limit: int = 80) -> dict[str, Any]:
        return _previous_history_response(
            deps,
            "main",
            before_seq=before_seq,
            limit=limit,
        )

    @app.get("/api/main/snapshot")
    def main_snapshot(client_id: str = "") -> dict[str, Any]:
        return _conversation_snapshot(deps, "main", client_id=client_id)

    @app.get("/api/main/events")
    def main_events(after_seq: int = 0, limit: int = 200) -> dict[str, Any]:
        return _conversation_events_response(
            deps,
            "main",
            after_seq=after_seq,
            limit=limit,
        )

    @app.get("/api/main/temporary-sessions")
    def main_temporary_sessions(limit: int = 50) -> dict[str, Any]:
        records = deps.conversation_controller.repository.list_temporary_conversations(
            limit=limit
        )
        return {
            "sessions": [_temporary_conversation_snapshot(record) for record in records]
        }

    @app.post("/api/main/temporary-sessions")
    async def main_create_temporary_session(request: Request) -> dict[str, Any]:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求体必须是对象")
        text = str(payload.get("text") or "").strip()
        create_action_id = str(payload.get("create_action_id") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="消息内容为空")
        if not create_action_id:
            raise HTTPException(status_code=400, detail="缺少创建动作编号")
        if _temporary_command_blocked(text):
            raise HTTPException(
                status_code=400,
                detail="临时对话不能切换、重置或删除永久主线会话。",
            )
        repository = deps.conversation_controller.repository
        record = repository.get_temporary_conversation_by_action(create_action_id)
        if record is None:
            parent = deps.session_manager.get_or_create(deps.main_ref)
            session = deps.session_manager.create_temporary_session(
                deps.main_ref,
                parent=parent,
            )
            record = repository.create_temporary_conversation(
                session_id=session.id,
                parent_session_id=parent.id,
                title=_temporary_conversation_title(text),
                create_action_id=create_action_id,
            )
        surface = _temporary_surface(record.session_id)
        submit_result = deps.conversation_controller.submit_input(
            _conversation_input_from_web_data(
                deps,
                surface,
                {
                    "text": text,
                    "client_action_id": create_action_id,
                    "raw_event_id": create_action_id,
                    "idempotency_key": create_action_id,
                },
            )
        )
        if submit_result.turn is not None:
            if submit_result.action == ConversationAction.ENQUEUE_MESSAGE:
                scheduler = _message_scheduler(deps, surface)
                if scheduler is None:
                    raise HTTPException(status_code=503, detail="服务启动中，请稍候重试")
                await _submit_enqueued_message_to_scheduler(
                    deps,
                    submit_result,
                    scheduler,
                    submit_result.turn,
                )
            elif submit_result.action == ConversationAction.RUN_COMMAND:
                scheduler = _command_scheduler(deps, surface, True)
                if scheduler is None:
                    raise HTTPException(status_code=503, detail="服务启动中，请稍候重试")
                await scheduler.submit(submit_result.turn)
            else:
                raise HTTPException(status_code=400, detail="该消息不能用于新建临时对话")
        return {
            "session": _temporary_conversation_snapshot(record),
            "message": (
                _message_snapshot(submit_result.message)
                if submit_result.message is not None
                else None
            ),
        }

    @app.get("/api/main/temporary-sessions/{session_id}/history")
    def main_temporary_history(
        session_id: str,
        before_seq: int = 0,
        limit: int = 80,
        filter: str = "all",
    ) -> dict[str, Any]:
        return _history_response(
            deps,
            _temporary_surface(session_id),
            before_seq=before_seq,
            limit=limit,
            history_filter=filter,
        )

    @app.get("/api/main/temporary-sessions/{session_id}/snapshot")
    def main_temporary_snapshot(session_id: str, client_id: str = "") -> dict[str, Any]:
        return _conversation_snapshot(
            deps,
            _temporary_surface(session_id),
            client_id=client_id,
        )

    @app.get("/api/main/temporary-sessions/{session_id}/events")
    def main_temporary_events(
        session_id: str,
        after_seq: int = 0,
        limit: int = 200,
    ) -> dict[str, Any]:
        return _conversation_events_response(
            deps,
            _temporary_surface(session_id),
            after_seq=after_seq,
            limit=limit,
        )

    @app.post("/api/main/messages/{message_id}/cancel")
    async def main_cancel_message(message_id: str) -> dict[str, Any]:
        return _http_action_response(
            await _cancel_queued_message_response(deps, "main", message_id),
            action="cancel_message",
            target_id=message_id,
        )

    @app.patch("/api/main/messages/{message_id}")
    async def main_edit_message(message_id: str, request: Request) -> dict[str, Any]:
        payload = await request.json()
        return _http_action_response(
            await _edit_queued_message_response(deps, "main", message_id, payload),
            action="edit_message",
            target_id=message_id,
        )

    @app.post("/api/main/messages/{message_id}/promote")
    async def main_promote_message(message_id: str) -> dict[str, Any]:
        return _http_action_response(
            await _promote_queued_message_response(deps, "main", message_id),
            action="promote_message",
            target_id=message_id,
        )

    @app.post("/api/main/messages/{message_id}/resume")
    async def main_resume_message(message_id: str) -> dict[str, Any]:
        return _http_action_response(
            await _resume_queued_message_response(deps, "main", message_id),
            action="resume_message",
            target_id=message_id,
        )

    @app.post("/api/main/messages/{message_id}/retry")
    async def main_retry_message(message_id: str) -> dict[str, Any]:
        return _http_action_response(
            await _retry_message_response(deps, "main", message_id),
            action="retry_message",
            target_id=message_id,
        )

    @app.post("/api/main/tool-calls/{tool_record_id}/cancel")
    def main_cancel_tool_call(tool_record_id: str) -> dict[str, Any]:
        return _http_action_response(
            _cancel_tool_call_response(deps, "main", tool_record_id),
            action="cancel_tool_call",
            target_id=tool_record_id,
        )

    @app.post("/api/main/approvals/{approval_id}/cancel")
    def main_cancel_approval(approval_id: str) -> dict[str, Any]:
        return _http_action_response(
            _cancel_approval_response(deps, "main", approval_id),
            action="cancel_approval",
            target_id=approval_id,
        )

    @app.post("/api/main/tool-calls/{tool_record_id}/retry")
    async def main_retry_tool_call(tool_record_id: str) -> dict[str, Any]:
        return _http_action_response(
            await _retry_tool_call_response(deps, "main", tool_record_id),
            action="retry_tool_call",
            target_id=tool_record_id,
        )

    if deps.assistant_service is not None:

        @app.get("/api/assistants")
        def assistants_list() -> dict[str, Any]:
            return {
                "assistants": [
                    _assistant_payload(deps, definition)
                    for definition in deps.assistant_service.registry.list()
                ],
                "available_tools": _assistant_available_tools(deps),
            }

        @app.post("/api/assistants")
        async def assistants_create(request: Request) -> dict[str, Any]:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="请求体必须是对象")
            definition = _create_custom_assistant(deps, payload)
            return {"ok": True, "assistant": _assistant_payload(deps, definition)}

        @app.put("/api/assistants/{assistant_id}")
        async def assistants_update(assistant_id: str, request: Request) -> dict[str, Any]:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="请求体必须是对象")
            definition = _update_custom_assistant(deps, assistant_id, payload)
            return {"ok": True, "assistant": _assistant_payload(deps, definition)}

        @app.delete("/api/assistants/{assistant_id}")
        def assistants_delete(assistant_id: str) -> dict[str, Any]:
            _delete_custom_assistant(deps, assistant_id)
            return {"ok": True}

        @app.get("/api/assistants/{assistant_id}/history")
        def assistant_history(
            assistant_id: str,
            before_seq: int = 0,
            limit: int = 80,
            filter: str = "all",
        ) -> dict[str, Any]:
            return _history_response(
                deps,
                _assistant_surface(deps, assistant_id),
                before_seq=before_seq,
                limit=limit,
                history_filter=filter,
            )

        @app.get("/api/assistants/{assistant_id}/previous-history")
        def assistant_previous_history(
            assistant_id: str, before_seq: int = 0, limit: int = 80
        ) -> dict[str, Any]:
            return _previous_history_response(
                deps,
                _assistant_surface(deps, assistant_id),
                before_seq=before_seq,
                limit=limit,
            )

        @app.get("/api/assistants/{assistant_id}/snapshot")
        def assistant_snapshot(assistant_id: str, client_id: str = "") -> dict[str, Any]:
            return _conversation_snapshot(
                deps,
                _assistant_surface(deps, assistant_id),
                client_id=client_id,
            )

        @app.get("/api/assistants/{assistant_id}/events")
        def assistant_events(
            assistant_id: str, after_seq: int = 0, limit: int = 200
        ) -> dict[str, Any]:
            return _conversation_events_response(
                deps,
                _assistant_surface(deps, assistant_id),
                after_seq=after_seq,
                limit=limit,
            )

        @app.post("/api/assistants/{assistant_id}/messages/{message_id}/cancel")
        async def assistant_cancel_message(assistant_id: str, message_id: str) -> dict[str, Any]:
            return _http_action_response(
                await _cancel_queued_message_response(
                    deps, _assistant_surface(deps, assistant_id), message_id
                ),
                action="cancel_message",
                target_id=message_id,
            )

        @app.patch("/api/assistants/{assistant_id}/messages/{message_id}")
        async def assistant_edit_message(
            assistant_id: str, message_id: str, request: Request
        ) -> dict[str, Any]:
            payload = await request.json()
            return _http_action_response(
                await _edit_queued_message_response(
                    deps, _assistant_surface(deps, assistant_id), message_id, payload
                ),
                action="edit_message",
                target_id=message_id,
            )

        @app.post("/api/assistants/{assistant_id}/messages/{message_id}/promote")
        async def assistant_promote_message(assistant_id: str, message_id: str) -> dict[str, Any]:
            return _http_action_response(
                await _promote_queued_message_response(
                    deps, _assistant_surface(deps, assistant_id), message_id
                ),
                action="promote_message",
                target_id=message_id,
            )

        @app.post("/api/assistants/{assistant_id}/messages/{message_id}/resume")
        async def assistant_resume_message(assistant_id: str, message_id: str) -> dict[str, Any]:
            return _http_action_response(
                await _resume_queued_message_response(
                    deps, _assistant_surface(deps, assistant_id), message_id
                ),
                action="resume_message",
                target_id=message_id,
            )

        @app.post("/api/assistants/{assistant_id}/messages/{message_id}/retry")
        async def assistant_retry_message(assistant_id: str, message_id: str) -> dict[str, Any]:
            return _http_action_response(
                await _retry_message_response(
                    deps, _assistant_surface(deps, assistant_id), message_id
                ),
                action="retry_message",
                target_id=message_id,
            )

        @app.post("/api/assistants/{assistant_id}/tool-calls/{tool_record_id}/cancel")
        def assistant_cancel_tool_call(assistant_id: str, tool_record_id: str) -> dict[str, Any]:
            return _http_action_response(
                _cancel_tool_call_response(
                    deps, _assistant_surface(deps, assistant_id), tool_record_id
                ),
                action="cancel_tool_call",
                target_id=tool_record_id,
            )

        @app.post("/api/assistants/{assistant_id}/approvals/{approval_id}/cancel")
        def assistant_cancel_approval(assistant_id: str, approval_id: str) -> dict[str, Any]:
            return _http_action_response(
                _cancel_approval_response(
                    deps, _assistant_surface(deps, assistant_id), approval_id
                ),
                action="cancel_approval",
                target_id=approval_id,
            )

        @app.post("/api/assistants/{assistant_id}/tool-calls/{tool_record_id}/retry")
        async def assistant_retry_tool_call(assistant_id: str, tool_record_id: str) -> dict[str, Any]:
            return _http_action_response(
                await _retry_tool_call_response(
                    deps, _assistant_surface(deps, assistant_id), tool_record_id
                ),
                action="retry_tool_call",
                target_id=tool_record_id,
            )

    if deps.timeline_view is not None:

        @app.get("/api/timeline/days")
        def timeline_days() -> dict[str, Any]:
            return {"days": deps.timeline_view.list_days()}

        @app.get("/api/timeline/overview")
        def timeline_overview(limit: int = 14) -> dict[str, Any]:
            items = deps.timeline_view.overview(limit=limit)
            return {"items": [asdict(item) for item in items]}

        @app.get("/api/timeline/day/{day}")
        def timeline_day(day: str) -> dict[str, Any]:
            cards = deps.timeline_view.day(day)
            breakdown = deps.timeline_view.day_breakdown(day)
            return {
                "day": day,
                "cards": [asdict(card) for card in cards],
                "breakdown": asdict(breakdown),
            }

        @app.get("/api/timeline/session/{session_id}")
        def timeline_session(session_id: str) -> dict[str, Any]:
            detail = deps.timeline_view.session_detail(session_id)
            if detail is None:
                raise HTTPException(status_code=404, detail="session not found")
            data = asdict(detail)
            for capture in data.get("captures", []):
                path = capture.pop("screenshot_path", "")
                capture["screenshot_url"] = (
                    f"/api/timeline/screenshot?path={quote(path)}" if path else ""
                )
            return data

        @app.get("/api/timeline/search")
        def timeline_search(q: str = "", limit: int = 50) -> dict[str, Any]:
            hits = deps.timeline_view.search(q, limit=limit)
            return {"hits": [asdict(hit) for hit in hits]}

        @app.get("/api/timeline/app-icon")
        def timeline_app_icon(bundle_id: str = "", app_name: str = "") -> FileResponse:
            path = deps.timeline_view.app_icon(bundle_id=bundle_id, app_name=app_name)
            if path is None or not path.is_file():
                raise HTTPException(status_code=404, detail="icon not found")
            return FileResponse(
                str(path),
                media_type="image/png",
                headers={"Cache-Control": "public, max-age=86400"},
            )

        @app.get("/api/timeline/screenshot")
        def timeline_screenshot(path: str) -> FileResponse:
            root = deps.screenshot_root
            if root is None:
                raise HTTPException(status_code=404, detail="screenshots disabled")
            try:
                resolved = Path(path).expanduser().resolve()
                resolved.relative_to(root.resolve())
            except (ValueError, OSError) as error:
                raise HTTPException(status_code=404, detail="not found") from error
            if not resolved.is_file():
                raise HTTPException(status_code=404, detail="not found")
            return FileResponse(str(resolved), headers={"Cache-Control": "no-store"})

        @app.post("/api/timeline/summary/{timeline_id}/retry")
        def timeline_retry(
            timeline_id: str,
            payload: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            # 失败重试按完整活动组复位，避免旧批次与组内较晚摘要倒序拼接。
            if deps.settings is None:
                raise HTTPException(status_code=503, detail="settings 不可用")
            from telos.memory_space.timeline import TimelineSummaryRepository
            from telos.timeline.repository import TimelineRepository

            paths = deps.settings.paths
            session_ids = [
                str(item or "").strip()
                for item in list((payload or {}).get("session_ids") or [])
                if str(item or "").strip()
            ]
            with TimelineRepository.for_agent_dir(paths.agent_dir) as repo:
                if not session_ids:
                    session_ids = repo.session_ids_for_summary(timeline_id)
                if not session_ids:
                    raise HTTPException(status_code=404, detail="未找到对应的时间线会话")
                try:
                    reset_result = repo.reset_session_group(session_ids=session_ids)
                except ValueError as error:
                    raise HTTPException(status_code=409, detail=str(error)) from error
                summary_ids = list(reset_result["summary_ids"])
                if timeline_id not in summary_ids:
                    summary_ids.append(timeline_id)
            with TimelineSummaryRepository.for_memory_space(paths.memory_space_dir) as summaries:
                for summary_id in summary_ids:
                    summaries.delete(summary_id)
            return {
                "ok": True,
                "reset_captures": reset_result["capture_count"],
                "reset_sessions": len(reset_result["session_ids"]),
                "reset_summaries": len(summary_ids),
            }

        @app.get("/api/activity/day/{day}")
        def activity_day(day: str) -> dict[str, Any]:
            if deps.settings is None:
                raise HTTPException(status_code=503, detail="settings 不可用")
            return asdict(
                activity_view.activity_day(
                    deps.settings.paths.agent_dir,
                    day,
                    min_uncovered_seconds=(
                        deps.settings.daemon.activity.min_uncovered_seconds
                    ),
                )
            )

        @app.get("/api/activity/{activity_id}")
        def activity_detail(activity_id: str) -> dict[str, Any]:
            if deps.settings is None:
                raise HTTPException(status_code=503, detail="settings 不可用")
            detail = activity_view.activity_detail(
                deps.settings.paths.agent_dir,
                activity_id,
                memory_space_dir=deps.settings.paths.memory_space_dir,
            )
            if detail is None:
                raise HTTPException(status_code=404, detail="activity not found")
            data = asdict(detail)
            for item in data.get("details", []):
                item["screenshot_urls"] = [
                    f"/api/timeline/screenshot?path={quote(path)}"
                    for path in item.pop("screenshot_paths", [])
                ]
            return data

        @app.post("/api/activity/revision/{revision_id}/undo")
        def activity_revision_undo(revision_id: str) -> dict[str, Any]:
            if deps.settings is None:
                raise HTTPException(status_code=503, detail="settings 不可用")
            try:
                action = activity_view.undo_activity_revision(
                    deps.settings.paths.agent_dir,
                    revision_id,
                    memory_space_dir=deps.settings.paths.memory_space_dir,
                )
            except ValueError as error:
                raise HTTPException(status_code=409, detail=str(error)) from error
            return {"ok": True, "action": action}

        @app.post("/api/activity/assign/{timeline_id}/retry")
        async def activity_assign_retry(timeline_id: str) -> dict[str, Any]:
            if deps.settings is None:
                raise HTTPException(status_code=503, detail="settings 不可用")
            try:
                result = await retry_failed_assign(
                    deps.settings,
                    timeline_id=timeline_id,
                )
            except ValueError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            return {
                "ok": not result.failed,
                "detail_count": result.detail_count,
                "created_count": result.created_count,
            }

    if deps.things_view is not None:

        @app.get("/api/things/agenda")
        def things_agenda(start: str = "", end: str = "", status: str = "open") -> dict[str, Any]:
            if not start or not end:
                start, end = _things_default_window()
            agenda = deps.things_view.agenda(start=start, end=end, status=status)
            return {
                "start": agenda.start,
                "end": agenda.end,
                "status": agenda.status,
                "items": [asdict(item) for item in agenda.items],
                "unscheduled": [asdict(item) for item in deps.things_view.unscheduled(status=status)],
                "containers": [asdict(c) for c in deps.things_view.containers()],
            }

        @app.get("/api/things/browse")
        def things_browse(status: str = "open") -> dict[str, Any]:
            browse = deps.things_view.browse(status=status)
            return {
                "status": browse.status,
                "items": [asdict(item) for item in browse.items],
                "containers": [asdict(c) for c in browse.containers],
            }

        @app.get("/api/things/detail/{item_id}")
        def things_detail(item_id: str) -> dict[str, Any]:
            item = deps.things_view.detail(item_id)
            if item is None:
                raise HTTPException(status_code=404, detail="thing not found")
            return item

        @app.get("/api/things/meta")
        def things_meta() -> dict[str, Any]:
            return deps.things_view.form_meta()

        @app.post("/api/things/items")
        async def things_create(request: Request) -> dict[str, Any]:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="请求体必须是对象")
            result = deps.things_view.create(payload)
            _raise_things_failure(result)
            created = result.get("created") or []
            return {"item": created[0] if created else None, "result": result}

        @app.patch("/api/things/items/{item_id}")
        async def things_update(item_id: str, request: Request) -> dict[str, Any]:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="请求体必须是对象")
            result = deps.things_view.update(item_id, payload)
            _raise_things_failure(result)
            updated = result.get("updated") or []
            return {"item": updated[0] if updated else None, "result": result}

        @app.delete("/api/things/items/{item_id}")
        def things_delete(item_id: str) -> dict[str, Any]:
            result = deps.things_view.delete(item_id)
            _raise_things_failure(result)
            deleted = result.get("deleted") or []
            return {"item": deleted[0] if deleted else None, "result": result}

        @app.post("/api/things/items/{item_id}/restore")
        async def things_restore_deleted(item_id: str, request: Request) -> dict[str, Any]:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="请求体必须是对象")
            result = deps.things_view.restore_deleted(item_id, str(payload.get("status") or "open"))
            _raise_things_failure(result)
            restored = result.get("restored") or []
            return {"item": restored[0] if restored else None, "result": result}

    if deps.recap_view is not None:

        @app.get("/api/recap/stream")
        def recap_stream() -> dict[str, Any]:
            return {"recaps": [asdict(item) for item in deps.recap_view.stream()]}

        @app.post("/api/recap/{recap_id}/retry")
        async def recap_retry(recap_id: str) -> dict[str, Any]:
            # 失败重试：删掉失败占位 recap，再就地（idle_seconds=0 不等空闲）重跑一次 idle
            # recap，为缺失区间重新生成。会阻塞到生成完成（含一次 LLM 调用）。
            if deps.settings is None:
                raise HTTPException(status_code=503, detail="settings 不可用")
            from telos.apps.worker import run_idle_recaps
            from telos.memory_space.recap import RecapRepository

            repo = RecapRepository.for_memory_space(deps.settings.paths.memory_space_dir)
            try:
                if not repo.delete(recap_id):
                    raise HTTPException(status_code=404, detail="recap 不存在")
            finally:
                repo.close()
            regenerated = await run_idle_recaps(deps.settings, idle_seconds=0)
            return {"ok": True, "regenerated": regenerated}

    if deps.log_view is not None:

        @app.get("/api/log/tail")
        def log_tail(limit: int = 400) -> dict[str, Any]:
            return {"lines": deps.log_view.tail(limit=max(1, min(2000, limit)))}

    if deps.settings is not None:

        @app.get("/api/settings")
        def settings_overview() -> dict[str, Any]:
            settings = _current_settings(deps)
            paths = settings.paths
            provider_entries = sorted(settings.providers.entries)
            return {
                "agent_id": paths.agent_id,
                "app_name": settings.app.name,
                "provider": deps.provider_name,
                "provider_default": settings.providers.default,
                "providers": provider_entries,
                "policy_mode": settings.tools.policy.mode,
                "max_steps": settings.agent_loop.max_steps,
                "memory_space": paths.memory_space_name,
                "paths": {
                    "config_dir": str(paths.config_dir),
                    "main_config": str(paths.main_config_path),
                    "agent_config": str(paths.agent_config_path),
                    "root_dir": str(paths.root_dir),
                    "agent_dir": str(paths.agent_dir),
                    "memory_space_dir": str(paths.memory_space_dir),
                },
                "features": {
                    "skills": bool(settings.skills.enabled),
                    "mcp": bool(settings.mcp.enabled),
                    "timeline": bool(settings.daemon.timeline.enabled),
                    "recap": bool(settings.memory.recap.enabled),
                },
                "files": _settings_file_summaries(deps),
            }

        @app.get("/api/settings/ui")
        def settings_ui() -> dict[str, Any]:
            configured = _current_settings(deps)
            return _settings_ui_runtime_payload(deps, configured)

        @app.put("/api/settings/ui")
        async def save_settings_ui(request: Request) -> dict[str, Any]:
            settings = _current_settings(deps)
            path = settings.paths.agent_config_path
            payload = await request.json()
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="请求体必须是对象")
            old_content = path.read_text(encoding="utf-8") if path.exists() else ""
            try:
                with file_lock(path):
                    update_settings_from_ui(settings, payload)
                    refreshed = load_settings(
                        settings.paths.main_config_path,
                        agent_id=settings.paths.agent_id,
                    )
            except Exception as error:  # noqa: BLE001
                _write_text_atomic(path, old_content)
                raise HTTPException(status_code=400, detail=f"配置校验失败：{error}") from error
            deps.coordinator.policy_mode = refreshed.tools.policy.mode
            deps.coordinator.tool_approvals = dict(refreshed.tools.approvals)
            settings_payload = _settings_ui_runtime_payload(deps, refreshed)
            return {
                "ok": True,
                "needs_restart": settings_payload["needs_restart"],
                "settings": settings_payload,
            }

        @app.post("/api/settings/agent/open")
        def open_agent_settings_file() -> dict[str, Any]:
            settings = _current_settings(deps)
            _open_file(settings.paths.agent_config_path)
            return {"ok": True, "path": str(settings.paths.agent_config_path)}

        @app.get("/api/settings/local-apps")
        def settings_local_apps() -> dict[str, Any]:
            return {"apps": local_apps_payload()}

        @app.get("/api/settings/permissions")
        def settings_permissions() -> dict[str, Any]:
            return permissions_status()

        @app.post("/api/settings/permissions/request")
        async def settings_permissions_request(request: Request) -> Any:
            payload = await request.json()
            kind = payload.get("kind") if isinstance(payload, dict) else None
            if kind not in PERMISSION_KINDS:
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": "kind 必须是 screen_recording 或 accessibility",
                        "fields": ["kind"],
                    },
                )
            return request_permission(kind)

        wechat_login = WeChatLoginSession()

        @app.get("/api/settings/wechat")
        def settings_wechat() -> dict[str, Any]:
            return _wechat_binding_state(deps)

        @app.post("/api/settings/wechat/login/start")
        async def settings_wechat_login_start() -> dict[str, Any]:
            # async：asyncio.create_task 需要运行中的事件循环（sync 端点跑在无 loop 的线程池里）。
            if wechat_login.task is not None and not wechat_login.task.done():
                return wechat_login.snapshot()
            from telos.adapters.wechat.config import WeChatConfig
            from telos.adapters.wechat.login import run_wechat_qr_login

            wechat_login.phase = "starting"
            wechat_login.qr_image = ""
            wechat_login.account = None
            wechat_login.error = ""
            store = _wechat_state_store(deps)

            def on_challenge(challenge: Any) -> None:
                wechat_login.qr_image = (
                    challenge.image_url or challenge.scan_data or challenge.qrcode
                )
                wechat_login.phase = "waiting"

            async def run() -> None:
                try:
                    result = await run_wechat_qr_login(
                        WeChatConfig.from_env(),
                        state_store=store,
                        challenge_callback=on_challenge,
                    )
                except Exception as error:  # noqa: BLE001 - 登录失败在 UI 展示，不该崩后端
                    wechat_login.phase = "error"
                    wechat_login.error = str(error) or "WeChat login failed"
                    return
                if result.ok and result.account is not None:
                    wechat_login.phase = "confirmed"
                    wechat_login.account = _public_wechat_account(result.account)
                else:
                    wechat_login.phase = "error"
                    wechat_login.error = result.error or "WeChat login failed"

            wechat_login.task = asyncio.create_task(run())
            return wechat_login.snapshot()

        @app.get("/api/settings/wechat/login/status")
        def settings_wechat_login_status() -> dict[str, Any]:
            return wechat_login.snapshot()

    @app.websocket("/ws")
    async def ws_chat(websocket: WebSocket) -> None:
        requested_surface = websocket.query_params.get("surface", "main")
        temporary_session_id = str(
            websocket.query_params.get("session_id") or ""
        ).strip()
        surface = (
            _temporary_surface(temporary_session_id)
            if requested_surface == "temporary" and temporary_session_id
            else requested_surface
        )
        if (
            requested_surface not in {"main", "temporary"}
            and not _is_assistant_surface(deps, requested_surface)
        ) or (
            requested_surface == "temporary" and not temporary_session_id
        ):
            await websocket.close(code=1008)
            return
        if requested_surface == "temporary":
            try:
                _surface_session(deps, surface)
            except HTTPException:
                await websocket.close(code=1008)
                return
        await websocket.accept()

        client_id = str(websocket.query_params.get("client_id") or "").strip()
        send_lock = asyncio.Lock()

        async def send_json(msg: dict[str, Any]) -> None:
            _attach_client_event_ack_to_snapshot(
                deps,
                surface=surface,
                client_id=client_id,
                message=msg,
            )
            async with send_lock:
                try:
                    await websocket.send_json(msg)
                except Exception:  # noqa: BLE001 - 连接已关闭：静默丢弃，别把异常抛回 turn
                    pass

        # 审批不再绑连接：请求经 live stream 的 `tool.call.approval_request` 事件送达（含
        # 助手委派进来的子 turn，其事件落在子 turn 流里、由 watcher 转发到本页面），Future
        # 存在进程级 registry，按 request_id 作答——切 tab/刷新/多窗口都不丢审批。
        # turn 作为独立 task 运行，reader 循环不阻塞——这样 approval / cancel_turn 动作
        # 能在 turn 进行中被立即处理（否则等待审批的 turn 会死锁）。
        turn_task: asyncio.Task | None = None
        turn_control: TurnStreamControl | None = None
        turn_cancel: CancelHandle | None = None
        conversation_events_task = asyncio.create_task(
            _watch_conversation_events(
                deps,
                surface,
                send_json,
                client_id=client_id,
            )
        )

        async def run_turn(
            turn: InboundTurn,
            control: TurnStreamControl,
            abort_check: CancelHandle,
        ) -> None:
            nonlocal turn_control, turn_cancel
            try:
                await _run_surface_turn(
                    deps,
                    surface,
                    turn,
                    send_json,
                    control=control,
                    abort_check=abort_check,
                )
            except Exception as error:  # noqa: BLE001
                await send_json(websocket_server_event("ui.error", message=str(error)))
            finally:
                if turn_control is control:
                    turn_control = None
                    turn_cancel = None

        def start_turn(turn: InboundTurn) -> None:
            nonlocal turn_task, turn_control, turn_cancel
            control = TurnStreamControl()
            abort_check = CancelHandle(preserve_records_on_abort=True)
            turn_control = control
            turn_cancel = abort_check
            turn_task = asyncio.create_task(run_turn(turn, control, abort_check))

        async def do_cancel(
            requested_turn_id: str,
            *,
            include_background: bool = False,
        ) -> tuple[bool, str]:
            turn_id = requested_turn_id.strip()
            if turn_control is not None and _should_cancel_local_turn_control(
                requested_turn_id=turn_id,
                control_turn_id=turn_control.turn_id,
            ):
                turn_control.cancel_requested = True
                if not turn_id:
                    turn_id = turn_control.turn_id
            if turn_cancel is not None and _should_cancel_local_turn_control(
                requested_turn_id=requested_turn_id,
                control_turn_id=turn_control.turn_id if turn_control is not None else "",
            ):
                turn_cancel.request(force=True)
            if not turn_id:
                turn_id = active_turn_id(
                    deps.runtime_view,
                    _surface_session_id(deps, surface),
                    include_child_turns=include_background
                    or _is_assistant_surface(deps, surface),
                )
            if turn_id:
                # preserve=True：web 终止任何 turn（含来自微信的 turn，其 handle 默认不保留）
                # 都保留已完成工具的部分上下文，与本端 web 终止一致。
                deps.runtime_view.cancel_turn(
                    turn_id,
                    preserve=True,
                    include_background=include_background,
                )
                await send_json(
                    websocket_server_event("turn.cancel_requested", turn_id=turn_id)
                )
                return True, turn_id
            # 找不到可取消的活跃 turn：统一队列模式下这很常见——本轮在投队列后、变成 live
            # turn 之前就快速失败/已结束，watcher 从未接到它。此时只回 turn.cancel_requested 会让
            # 前端死等一个永不到来的 turn.completed，卡在“终止中…”。补一个终态让 UI 复位。
            await send_json(
                websocket_server_event(
                    "turn.completed",
                    turn_id="",
                    text="",
                    aborted=True,
                )
            )
            return False, ""

        async def cancel_current(
            requested_turn_id: str = "",
            turn: InboundTurn | None = None,
            cancel_target: CancelTarget = CancelTarget.CURRENT_FOREGROUND_TREE,
            control_action: str = "",
            client_action_id: str = "",
        ) -> tuple[bool, str]:
            include_background = cancel_target == CancelTarget.ENTIRE_AGENT_TREE
            cancelled, resolved_turn_id = await do_cancel(
                requested_turn_id,
                include_background=include_background,
            )
            session_id = str(
                ((turn.metadata if turn is not None else {}) or {}).get(
                    "conversation_session_id"
                )
                or _surface_session_id(deps, surface)
            )
            if deps.approval_registry is not None and resolved_turn_id:
                turn_ids = deps.runtime_view.list_cancel_tree_turn_ids(
                    resolved_turn_id,
                    include_background=include_background,
                )
                if not turn_ids:
                    turn_ids = [resolved_turn_id]
                cancelled_approvals = _cancel_pending_approvals_by_stop(
                    deps,
                    surface,
                    turn_ids=turn_ids,
                    session_id=session_id,
                    feedback="cancelled by stop",
                )
                cancelled = bool(cancelled_approvals) or cancelled
            if deps.question_registry is not None and resolved_turn_id:
                turn_ids = deps.runtime_view.list_cancel_tree_turn_ids(
                    resolved_turn_id,
                    include_background=include_background,
                )
                if not turn_ids:
                    turn_ids = [resolved_turn_id]
                cancelled_questions = cancel_pending_questions_by_stop(
                    deps.question_registry,
                    deps.conversation_controller,
                    ref=_surface_ref(deps, surface),
                    turn_ids=turn_ids,
                    session_id=session_id,
                    source="web",
                    surface=surface,
                    feedback="cancelled by stop",
                )
                cancelled = bool(cancelled_questions) or cancelled
            cancel_turn = turn or InboundTurn(
                ref=_surface_ref(deps, surface),
                text="/stop",
                metadata={"conversation_session_id": session_id},
            )
            command_scheduler = (
                deps.primary_command_handle.scheduler
                if deps.primary_command_handle is not None
                else None
            )
            if (
                command_scheduler is not None
                and _cancel_target_cancels_session_schedulers(cancel_target)
            ):
                cancelled = await command_scheduler.cancel_for_turn(
                    cancel_turn,
                    force=True,
                ) or cancelled
            scheduler = _message_scheduler(deps, surface)
            if (
                scheduler is not None
                and _cancel_target_cancels_session_schedulers(cancel_target)
            ):
                cancelled = await scheduler.cancel_for_turn(
                    cancel_turn,
                    force=True,
                ) or cancelled
            resolved_target_id = _resolved_stop_target_id(
                cancelled=cancelled,
                session_id=session_id,
                requested_turn_id=requested_turn_id,
                resolved_turn_id=resolved_turn_id,
            )
            cancel_tree = _runtime_cancel_tree_snapshot(
                deps.runtime_view,
                resolved_turn_id or requested_turn_id,
                include_background=include_background,
            )
            deps.conversation_controller.record_stop_result(
                session_id=session_id,
                cancelled=cancelled,
                cancel_target=cancel_target.value,
                requested_turn_id=requested_turn_id or resolved_turn_id,
                target_id=resolved_target_id,
                source="web",
                control_action=control_action,
                cancel_tree=cancel_tree,
                client_action_id=client_action_id,
            )
            return cancelled, resolved_target_id

        # 两层交付：canonical 权威时间线走 conversation.event（上面的 _watch_conversation_events），
        # 它是唯一权威、按会话 seq 有序幂等、覆盖本 session 所有 turn（含子 turn / 外来端）。
        # 这里只跑「一个」overlay 生产者转发 token deltas 做打字动画——单生产者、按 turn_id 去重，
        # 无竞态、无重复推流。session_id 每轮重解析以跟随 /new /reset 的会话轮换。
        #
        # scheduled_main：main 普通消息投持久调度器；turn 真正起来后同样被这个 overlay 生产者接上。
        scheduled_main = _is_main_surface(surface) and (
            deps.local_message_scheduler is not None
            or deps.primary_scheduler_handle is not None
        )
        streamed: set[str] = set()
        current_session_id = lambda: _surface_session_id(deps, surface)  # noqa: E731
        delta_task: asyncio.Task | None = asyncio.create_task(
            forward_live_deltas(
                session_id_fn=current_session_id,
                runtime_view=deps.runtime_view,
                send_json=send_json,
                streamed=streamed,
            )
        )

        try:
            while True:
                data = await websocket.receive_json()
                if not isinstance(data, dict):
                    continue
                action = _websocket_client_action(data)
                if not action:
                    await send_json(
                        _action_message_event(
                            "ui.error",
                            action="",
                            client_action_id=str(
                                data.get("client_action_id") or ""
                            ),
                            target_id=_action_target_id(data),
                            message="未知客户端动作。",
                            ok=False,
                            code="unknown_client_action",
                        )
                    )
                    continue
                if action == "approval":
                    await _handle_websocket_approval_response(
                        deps,
                        surface,
                        data,
                        send_json,
                    )
                    continue
                if action == "input_request_response":
                    await _handle_websocket_input_request_response(
                        deps,
                        surface,
                        data,
                        send_json,
                    )
                    continue
                if action == "cancel_input_request":
                    await _handle_websocket_input_request_response(
                        deps,
                        surface,
                        {**data, "decision": "cancel"},
                        send_json,
                        action_name="cancel_input_request",
                    )
                    continue
                if action == "cancel_approval":
                    await _handle_websocket_approval_cancel(
                        deps,
                        surface,
                        data,
                        send_json,
                    )
                    continue
                if action == "cancel_turn":
                    client_action_id = str(data.get("client_action_id") or "").strip()
                    target_id = str(data.get("turn_id") or "").strip()
                    _record_web_agent_control_input(
                        deps,
                        surface,
                        data,
                        control_action="cancel_turn",
                        cancel_target=CancelTarget.CURRENT_FOREGROUND_TREE,
                    )
                    cancelled, resolved_target_id = await cancel_current(
                        target_id,
                        control_action="cancel_turn",
                        client_action_id=client_action_id,
                    )
                    await send_json(
                        _stop_action_event(
                            surface=surface,
                            action="cancel_turn",
                            client_action_id=client_action_id,
                            target_id=resolved_target_id or target_id,
                            cancelled=cancelled,
                            turn_id=target_id or resolved_target_id,
                            cancel_tree=_runtime_cancel_tree_snapshot(
                                deps.runtime_view,
                                target_id or resolved_target_id,
                                include_background=False,
                            ),
                        )
                    )
                    continue
                if action == "cancel_agent":
                    await _handle_websocket_agent_cancel(
                        deps,
                        surface,
                        data,
                        send_json,
                        cancel_current=cancel_current,
                    )
                    continue
                if action == "cancel_agent_tree":
                    client_action_id = str(data.get("client_action_id") or "").strip()
                    target_id = str(data.get("turn_id") or "").strip()
                    _record_web_agent_control_input(
                        deps,
                        surface,
                        data,
                        control_action="cancel_agent_tree",
                        cancel_target=CancelTarget.ENTIRE_AGENT_TREE,
                    )
                    cancelled, resolved_target_id = await cancel_current(
                        target_id,
                        cancel_target=CancelTarget.ENTIRE_AGENT_TREE,
                        control_action="cancel_agent_tree",
                        client_action_id=client_action_id,
                    )
                    await send_json(
                        _stop_action_event(
                            surface=surface,
                            action="cancel_agent_tree",
                            client_action_id=client_action_id,
                            target_id=resolved_target_id or target_id,
                            cancelled=cancelled,
                            turn_id=target_id or resolved_target_id,
                            cancel_tree=_runtime_cancel_tree_snapshot(
                                deps.runtime_view,
                                target_id or resolved_target_id,
                                include_background=True,
                            ),
                        )
                    )
                    continue
                if action == "cancel_tool_call":
                    await _handle_websocket_tool_cancel(
                        deps,
                        surface,
                        data,
                        send_json,
                    )
                    continue
                if action == "cancel_background_task":
                    client_action_id = str(data.get("client_action_id") or "").strip()
                    target_id = str(data.get("task_name") or "").strip()
                    _record_web_agent_control_input(
                        deps,
                        surface,
                        data,
                        control_action="cancel_background_task",
                        cancel_target=CancelTarget.BACKGROUND_TASK,
                        target_id=target_id,
                    )
                    handle = deps.background_scheduler_handle
                    cancel_active = getattr(handle, "cancel_active", None)
                    cancelled = bool(cancel_active()) if callable(cancel_active) else False
                    _record_web_stop_result(
                        deps,
                        session_id=_surface_session_id(deps, surface),
                        cancelled=cancelled,
                        cancel_target=CancelTarget.BACKGROUND_TASK.value,
                        target_id=target_id,
                        source="web",
                        control_action="cancel_background_task",
                        client_action_id=client_action_id,
                    )
                    await send_json(
                        _action_message_event(
                            "ui.notice" if cancelled else "ui.error",
                            action="cancel_background_task",
                            client_action_id=client_action_id,
                            target_id=target_id,
                            message=(
                                "已请求停止后台任务。"
                                if cancelled
                                else "没有正在运行的后台任务。"
                            ),
                            ok=cancelled,
                            reason="cancelled" if cancelled else "no_running_task",
                            code="" if cancelled else "background_task_not_running",
                        )
                    )
                    continue
                if action == "ack_event_position":
                    _record_web_event_ack(deps, surface, data)
                    continue
                if action in _WEBSOCKET_QUEUE_ACTIONS:
                    await _handle_websocket_queue_action(
                        deps,
                        surface,
                        action,
                        data,
                        send_json,
                    )
                    continue
                if action in {
                    "send",
                    "append_current_turn",
                    "save_draft",
                    "switch_session",
                    "confirm_command",
                }:
                    if action == "confirm_command":
                        payload = await _resolve_command_confirmation_action(
                            deps,
                            surface,
                            data,
                            send_json,
                        )
                        if payload is None:
                            continue
                    else:
                        payload = _websocket_action_payload(data)
                    text = str(payload.get("text") or "").strip()
                    if not text:
                        client_action_id = str(
                            payload.get("client_action_id")
                            or data.get("client_action_id")
                            or ""
                        ).strip()
                        if action == "switch_session":
                            await send_json(
                                _action_message_event(
                                    "ui.error",
                                    action=action,
                                    client_action_id=client_action_id,
                                    target_id=_action_target_id(data),
                                    message="无效的会话切换动作。",
                                    ok=False,
                                    code="invalid_switch_session",
                                )
                            )
                        elif action != "confirm_command":
                            await send_json(
                                _action_message_event(
                                    "ui.error",
                                    action=action,
                                    client_action_id=client_action_id,
                                    target_id=_action_target_id(data),
                                    message="消息内容为空。",
                                    ok=False,
                                    code="empty_message",
                                )
                            )
                        continue
                    if _temporary_surface_session_id(surface) and _temporary_command_blocked(text):
                        await send_json(
                            _action_message_event(
                                "ui.error",
                                action=action,
                                client_action_id=str(
                                    payload.get("client_action_id") or ""
                                ),
                                target_id="",
                                message="临时对话不能切换、重置或删除永久主线会话。",
                                ok=False,
                                code="temporary_command_blocked",
                            )
                        )
                        continue
                    submit_result = deps.conversation_controller.submit_input(
                        _conversation_input_from_web_data(deps, surface, payload)
                    )
                    if submit_result.action == ConversationAction.CANCEL_CURRENT:
                        client_action_id = str(
                            payload.get("client_action_id") or ""
                        ).strip()
                        cancel_target = (
                            submit_result.classification.cancel_target
                            or CancelTarget.CURRENT_FOREGROUND_TREE
                        )
                        cancelled, resolved_target_id = await cancel_current(
                            "",
                            submit_result.turn,
                            cancel_target=cancel_target,
                            control_action=action
                            if action
                            in {"cancel_turn", "cancel_agent_tree"}
                            else "",
                        )
                        cancel_tree = _runtime_cancel_tree_snapshot(
                            deps.runtime_view,
                            resolved_target_id,
                            include_background=cancel_target
                            == CancelTarget.ENTIRE_AGENT_TREE,
                        )
                        if not cancelled:
                            await send_json(
                                _stop_action_event(
                                    surface=surface,
                                    action=action,
                                    client_action_id=client_action_id,
                                    target_id=str(
                                        getattr(submit_result.message, "message_id", "")
                                        or resolved_target_id
                                        or ""
                                    ),
                                    cancelled=False,
                                    cancel_tree=cancel_tree,
                                )
                            )
                        else:
                            await send_json(
                                _stop_action_event(
                                    surface=surface,
                                    action=action,
                                    client_action_id=client_action_id,
                                    target_id=resolved_target_id
                                    or str(
                                        getattr(submit_result.message, "message_id", "")
                                        or ""
                                    ),
                                    cancelled=True,
                                    turn_id=resolved_target_id,
                                    cancel_tree=cancel_tree,
                                )
                            )
                        continue
                    if submit_result.turn is None:
                        if submit_result.message is not None:
                            client_action_id = str(
                                payload.get("client_action_id") or ""
                            )
                            await send_json(
                                _action_success_event(
                                    surface=surface,
                                    action=action,
                                    client_action_id=client_action_id,
                                    target_id=submit_result.message.message_id,
                                    result={
                                        "ok": True,
                                        "duplicate": True,
                                        "message": _message_snapshot(
                                            submit_result.message
                                        ),
                                    },
                                )
                            )
                            await _send_conversation_snapshot(deps, surface, send_json)
                            continue
                        await send_json(
                            _action_message_event(
                                "ui.notice",
                                action=action,
                                client_action_id=str(
                                    payload.get("client_action_id") or ""
                                ),
                                target_id="",
                                message="已记录。",
                                ok=True,
                                reason="recorded",
                            )
                        )
                        continue
                    turn = submit_result.turn
                    if submit_result.action == ConversationAction.ENQUEUE_MESSAGE:
                        scheduler = _message_scheduler(deps, surface)
                        if scheduler is not None:
                            await _submit_enqueued_message_to_scheduler(
                                deps,
                                submit_result,
                                scheduler,
                                turn,
                            )
                            await send_json(
                                _turn_queued_event(submit_result, action=action)
                            )
                            continue
                        if scheduled_main:
                            await send_json(
                                _action_message_event(
                                    "ui.error",
                                    action=action,
                                    client_action_id=str(
                                        payload.get("client_action_id") or ""
                                    ),
                                    target_id=str(
                                        getattr(submit_result.message, "message_id", "")
                                        or ""
                                    ),
                                    message="服务启动中，请稍候重试",
                                    ok=False,
                                    code="scheduler_unavailable",
                                    retryable=True,
                                )
                            )
                            continue
                    command_scheduler = _command_scheduler(deps, surface, scheduled_main)
                    if submit_result.action == ConversationAction.RUN_COMMAND:
                        blocked = _running_command_blocked_reason(
                            deps,
                            surface,
                            submit_result,
                        )
                        if blocked:
                            if _command_requires_running_confirmation(submit_result):
                                _create_command_confirmation_request(
                                    deps,
                                    surface,
                                    submit_result,
                                    turn,
                                    blocked,
                                )
                                await send_json(
                                    _action_message_event(
                                        "ui.notice",
                                        action=action,
                                        client_action_id=str(
                                            payload.get("client_action_id") or ""
                                        ),
                                        target_id=str(
                                            getattr(
                                                submit_result.message,
                                                "message_id",
                                                "",
                                            )
                                            or ""
                                        ),
                                        message="需要确认后再执行命令。",
                                        ok=True,
                                        reason="confirmation_required",
                                    )
                                )
                                await _send_conversation_snapshot(deps, surface, send_json)
                            else:
                                await send_json(
                                    _action_message_event(
                                        "ui.notice",
                                        action=action,
                                        client_action_id=str(
                                            payload.get("client_action_id") or ""
                                        ),
                                        target_id=str(
                                            getattr(
                                                submit_result.message,
                                                "message_id",
                                                "",
                                            )
                                            or ""
                                        ),
                                        message=blocked,
                                        ok=True,
                                        reason="command_blocked",
                                    )
                                )
                            continue
                    if command_scheduler is not None:
                        # 注册命令走独立命令调度器；普通模型消息走持久会话调度器。
                        await command_scheduler.submit(turn)
                        await send_json(
                            _turn_queued_event(submit_result, action=action)
                        )
                        continue
                    if (
                        scheduled_main
                        and submit_result.action != ConversationAction.ENQUEUE_MESSAGE
                        and deps.local_message_scheduler is None
                        and deps.primary_command_handle is not None
                    ):
                        await send_json(
                            _action_message_event(
                                "ui.error",
                                action=action,
                                client_action_id=str(
                                    payload.get("client_action_id") or ""
                                ),
                                target_id=str(
                                    getattr(submit_result.message, "message_id", "")
                                    or ""
                                ),
                                message="服务启动中，请稍候重试",
                                ok=False,
                                code="command_scheduler_unavailable",
                                retryable=True,
                            )
                        )
                        continue
                    if turn_task is not None and not turn_task.done():
                        await send_json(
                            _action_message_event(
                                "ui.error",
                                action=action,
                                client_action_id=str(
                                    payload.get("client_action_id") or ""
                                ),
                                target_id=str(
                                    getattr(submit_result.message, "message_id", "")
                                    or ""
                                ),
                                message="上一轮还在进行中",
                                ok=False,
                                code="turn_already_running",
                                retryable=True,
                            )
                        )
                        continue
                    start_turn(turn)
                    continue
                await send_json(
                    _action_message_event(
                        "ui.error",
                        action=action,
                        client_action_id=str(data.get("client_action_id") or ""),
                        target_id=_action_target_id(data),
                        message=f"不支持的客户端动作：{action}",
                        ok=False,
                        code="unsupported_client_action",
                    )
                )
        except WebSocketDisconnect:
            # 断连只是脱离取景器：不再 deny 待审批（留到被答或自身超时），也不取消 turn。
            return
        finally:
            background_tasks = [
                task
                for task in (delta_task, conversation_events_task)
                if task is not None
            ]
            for task in background_tasks:
                if not task.done():
                    task.cancel()
            if background_tasks:
                await asyncio.gather(*background_tasks, return_exceptions=True)

    if GUI_APP_DIR.is_dir():
        # 新 GUI 在根路径提供，注册在所有 /api 与 /ws 路由之后：只兜底非接口
        # 路径；非文件路径回退到 index.html（SPA 客户端路由）。
        @app.get("/", include_in_schema=False)
        @app.get("/{path:path}", include_in_schema=False)
        def gui_app(path: str = "") -> FileResponse:
            return _gui_app_response(path)

    return app


def _things_default_window() -> tuple[str, str]:
    from telos.views.things_view import default_window

    return default_window(days=7)


def _raise_things_failure(result: dict[str, Any]) -> None:
    """create/update 结果里 failed[] 非空 → 4xx，detail 用首条错误。"""
    failed = result.get("failed") or []
    if failed:
        error = str((failed[0] or {}).get("error") or "写入失败")
        raise HTTPException(status_code=400, detail=error)


def _settings_file_summaries(deps: WebDeps) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    seen: set[Path] = set()
    for file_id, label, path in _settings_files(deps):
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        files.append({"id": file_id, "label": label, "path": str(path)})
    return files


def _settings_files(deps: WebDeps) -> list[tuple[str, str, Path]]:
    settings = deps.settings
    if settings is None:
        return []
    paths = settings.paths
    return [
        ("agent", "Agent 配置", paths.agent_config_path),
    ]


def _current_settings(deps: WebDeps) -> Any:
    settings = deps.settings
    if settings is None:
        raise HTTPException(status_code=404, detail="settings unavailable")
    return load_settings(settings.paths.main_config_path, agent_id=settings.paths.agent_id)


_ASSISTANT_PROMPT_DIR = "assistant_prompts"

# 写入 assistants 配置时的审批默认值：只读工具 approval: false，写类工具 approval: true。
_READ_ONLY_TOOL_NAMES = {
    "Read",
    "thingsList",
    "thingsDetail",
    "scheduleList",
    "web_search",
    "get_weather",
}


def _assistant_payload(deps: WebDeps, definition: Any) -> dict[str, Any]:
    try:
        profile = deps.assistant_service.registry.profile_for(definition)
        tools = list(profile.tools.enabled)
        system_prompt = "" if definition.builtin else profile.prompt_text
    except Exception:  # noqa: BLE001 - 单个助手配置损坏不影响列表接口
        tools = []
        system_prompt = ""
    return {
        "id": definition.id,
        "label": definition.label,
        "description": definition.description,
        "builtin": definition.builtin,
        "tools": tools,
        "system_prompt": system_prompt,
    }


def _assistant_available_tools(deps: WebDeps) -> list[dict[str, str]]:
    """助手编辑器可分配的工具清单；settings 不可用时退化为空列表。"""
    try:
        settings = _current_settings(deps)
    except HTTPException:
        return []
    return [
        {
            "id": name,
            "name": name,
            "description": str(getattr(entry, "description", "") or ""),
        }
        for name, entry in settings.tools.entries.items()
    ]


def _assistant_config_entry(assistant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    tools_raw = payload.get("tools")
    if not isinstance(tools_raw, list) or not tools_raw:
        raise HTTPException(status_code=400, detail="tools 必须是非空数组")
    entries: dict[str, Any] = {}
    for item in tools_raw:
        name = str(item or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="工具名不能为空")
        entries[name] = {
            "enabled": True,
            "approval": name not in _READ_ONLY_TOOL_NAMES,
        }
    return {
        "name": assistant_id,
        "label": str(payload.get("label") or "").strip() or assistant_id,
        "description": str(payload.get("description") or "").strip(),
        "system_prompt": {
            "prompt_template": f"{_ASSISTANT_PROMPT_DIR}/{assistant_id}.md",
        },
        "tools": {"entries": entries},
    }


def _create_custom_assistant(deps: WebDeps, payload: dict[str, Any]) -> Any:
    from telos.core.errors import ConfigurationError

    service = deps.assistant_service
    try:
        assistant_id = validate_assistant_id(
            payload.get("id"),
            taken=set(service.registry.ids()),
        )
    except ConfigurationError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    prompt_text = str(payload.get("system_prompt") or "").strip()
    if not prompt_text:
        raise HTTPException(status_code=400, detail="system_prompt 不能为空")
    entry = _assistant_config_entry(assistant_id, payload)
    settings = _current_settings(deps)
    prompt_path = (
        settings.paths.config_base_dir / _ASSISTANT_PROMPT_DIR / f"{assistant_id}.md"
    )

    def mutate(raw: dict[str, Any]) -> None:
        assistants = dict(raw.get("assistants") or {})
        assistants[assistant_id] = entry
        raw["assistants"] = assistants

    registry = _write_assistants_config(
        deps,
        settings,
        mutate,
        prompt_writes=[(prompt_path, prompt_text, None)],
        validate_ids=[assistant_id],
    )
    return registry.get(assistant_id)


def _update_custom_assistant(deps: WebDeps, assistant_id: str, payload: dict[str, Any]) -> Any:
    service = deps.assistant_service
    clean_id = str(assistant_id or "").strip()
    if not service.registry.has(clean_id):
        raise HTTPException(status_code=404, detail=f"未知助手：{clean_id or '-'}")
    if service.registry.get(clean_id).builtin:
        raise HTTPException(status_code=400, detail="内置助手不能修改")
    prompt_text = str(payload.get("system_prompt") or "").strip()
    if not prompt_text:
        raise HTTPException(status_code=400, detail="system_prompt 不能为空")
    entry = _assistant_config_entry(clean_id, payload)
    settings = _current_settings(deps)
    prompt_path = settings.paths.config_base_dir / _ASSISTANT_PROMPT_DIR / f"{clean_id}.md"
    old_prompt = (
        prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else None
    )

    def mutate(raw: dict[str, Any]) -> None:
        assistants = dict(raw.get("assistants") or {})
        assistants[clean_id] = entry
        raw["assistants"] = assistants

    registry = _write_assistants_config(
        deps,
        settings,
        mutate,
        prompt_writes=[(prompt_path, prompt_text, old_prompt)],
        validate_ids=[clean_id],
    )
    return registry.get(clean_id)


def _delete_custom_assistant(deps: WebDeps, assistant_id: str) -> None:
    service = deps.assistant_service
    clean_id = str(assistant_id or "").strip()
    if not service.registry.has(clean_id):
        raise HTTPException(status_code=404, detail=f"未知助手：{clean_id or '-'}")
    if service.registry.get(clean_id).builtin:
        raise HTTPException(status_code=400, detail="内置助手不能删除")
    settings = _current_settings(deps)
    prompt_path = settings.paths.config_base_dir / _ASSISTANT_PROMPT_DIR / f"{clean_id}.md"
    old_prompt = (
        prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else None
    )

    def mutate(raw: dict[str, Any]) -> None:
        assistants = dict(raw.get("assistants") or {})
        assistants.pop(clean_id, None)
        raw["assistants"] = assistants

    # 只删配置与提示词文件；磁盘上的会话数据保留。
    _write_assistants_config(
        deps,
        settings,
        mutate,
        prompt_deletes=[(prompt_path, old_prompt)],
    )


def _write_assistants_config(
    deps: WebDeps,
    settings: Any,
    mutate: Callable[[dict[str, Any]], None],
    *,
    prompt_writes: list[tuple[Path, str, str | None]] = (),
    prompt_deletes: list[tuple[Path, str | None]] = (),
    validate_ids: list[str] = (),
) -> Any:
    """改写 agent config 的 assistants 节并使之生效。

    沿用 /api/settings/ui 的模式：file_lock + 原子写 + reload 校验 + 失败回滚。
    ``prompt_writes`` 是 (路径, 新内容, 旧内容) 三元组，旧内容为 None 表示文件原先
    不存在、回滚时删除；``prompt_deletes`` 是 (路径, 旧内容)，回滚时按旧内容恢复。
    """
    path = settings.paths.agent_config_path
    old_content = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        for prompt_path, new_text, _old_text in prompt_writes:
            prompt_path.parent.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(new_text, encoding="utf-8")
        for prompt_path, _old_text in prompt_deletes:
            prompt_path.unlink(missing_ok=True)
        with file_lock(path):
            raw = load_config_mapping(path)
            mutate(raw)
            write_config_mapping(path, raw)
            refreshed = load_settings(
                settings.paths.main_config_path,
                agent_id=settings.paths.agent_id,
            )
            registry = AssistantRegistry.from_settings(refreshed)
            for assistant_id in validate_ids:
                registry.profile_for(registry.get(assistant_id))
    except Exception as error:
        _write_text_atomic(path, old_content)
        for prompt_path, _new_text, old_text in prompt_writes:
            if old_text is None:
                prompt_path.unlink(missing_ok=True)
            else:
                prompt_path.write_text(old_text, encoding="utf-8")
        for prompt_path, old_text in prompt_deletes:
            if old_text is not None:
                prompt_path.parent.mkdir(parents=True, exist_ok=True)
                prompt_path.write_text(old_text, encoding="utf-8")
        raise HTTPException(
            status_code=400, detail=f"助手配置写入失败：{error}"
        ) from error
    deps.assistant_service.reload_registry(registry)
    return registry


def _settings_ui_runtime_payload(deps: WebDeps, configured: Any) -> dict[str, Any]:
    payload = settings_ui_payload(configured)
    payload["needs_restart"] = settings_restart_required(deps.settings, configured)
    return payload


def _wechat_state_store(deps: WebDeps) -> Any:
    from telos.adapters.wechat.state import WeChatStateStore
    from telos.memory_space.paths import AgentRuntimeLayout

    layout = AgentRuntimeLayout.for_agent_dir(deps.settings.paths.agent_dir)
    return WeChatStateStore(layout.runtime_wechat_dir)


def _public_wechat_account(account: Any) -> dict[str, str] | None:
    # 脱敏：只回展示所需字段，绝不外泄 token。
    if account is None:
        return None
    return {
        "account_id": str(getattr(account, "account_id", "") or ""),
        "user_id": str(getattr(account, "user_id", "") or ""),
        "base_url": str(getattr(account, "base_url", "") or ""),
        "saved_at": str(getattr(account, "saved_at", "") or ""),
    }


def _wechat_enabled(deps: WebDeps) -> bool:
    enabled = getattr(getattr(deps.settings, "adapters", None), "enabled", None) or []
    return "wechat" in [str(item).strip() for item in enabled]


def _wechat_binding_state(deps: WebDeps) -> dict[str, Any]:
    account = _wechat_state_store(deps).load_default_account()
    return {
        "enabled": _wechat_enabled(deps),
        "linked": account is not None,
        "account": _public_wechat_account(account),
    }


def _runtime_supervisor(deps: WebDeps) -> AgentRuntimeSupervisor:
    if deps.runtime_supervisor is not None:
        return deps.runtime_supervisor
    settings = _current_settings(deps)
    return AgentRuntimeSupervisor(
        agent_dir=settings.paths.agent_dir,
        agent_id=settings.paths.agent_id,
    )


def _runtime_control_response(
    deps: WebDeps,
    payload: dict[str, Any],
) -> dict[str, Any]:
    supervisor = _runtime_supervisor(deps)
    response = request_runtime_control(supervisor.control_socket_path, payload)
    if response is None:
        raise HTTPException(status_code=503, detail="runtime control socket unavailable")
    if not response.get("ok"):
        raise HTTPException(status_code=409, detail=str(response.get("error") or "runtime control failed"))
    return response


def _write_text_atomic(path: Path, content: str) -> None:
    with file_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(path.parent),
            delete=False,
        ) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        tmp_path.replace(path)


def _open_file(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])  # noqa: S603,S607 - 本地设置页明确要求打开文件
        return
    if sys.platform.startswith("win"):
        subprocess.Popen(["cmd", "/c", "start", "", str(path)])  # noqa: S603,S607
        return
    subprocess.Popen(["xdg-open", str(path)])  # noqa: S603,S607


def _surface_session_id(deps: WebDeps, surface: str) -> str:
    return _surface_session(deps, surface).id


def _surface_session(deps: WebDeps, surface: str) -> Any:
    temporary_session_id = _temporary_surface_session_id(surface)
    if temporary_session_id:
        repository = deps.conversation_controller.repository
        if repository.get_temporary_conversation(temporary_session_id) is None:
            raise HTTPException(status_code=404, detail="临时对话不存在")
        session = deps.session_manager.load(temporary_session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="临时对话会话不存在")
        return session
    if _is_assistant_surface(deps, surface):
        service = deps.assistant_service
        return service.sessions_for(surface).active_session(
            service.session_manager, _assistant_ref(surface)
        )
    return deps.session_manager.get_or_create(deps.main_ref)


def _assistant_ids(deps: WebDeps) -> list[str]:
    service = deps.assistant_service
    if service is None:
        return []
    return service.registry.ids()


def _is_assistant_surface(deps: WebDeps, surface: str) -> bool:
    service = deps.assistant_service
    if service is None:
        return False
    return service.registry.has(str(surface or "").strip())


def _assistant_surface(deps: WebDeps, assistant_id: str) -> str:
    clean = str(assistant_id or "").strip()
    if not _is_assistant_surface(deps, clean):
        raise HTTPException(status_code=404, detail=f"未知助手：{clean or '-'}")
    return clean


def _assistant_ref(surface: str) -> ConversationRef:
    return ConversationRef(
        platform="web", chat_id=surface, user_id="local", source_id=surface
    )


def _temporary_surface(session_id: str) -> str:
    return f"temporary:{str(session_id or '').strip()}"


def _temporary_conversation_snapshot(record: Any) -> dict[str, Any]:
    return {
        "id": str(record.session_id),
        "parent_session_id": str(record.parent_session_id),
        "title": str(record.title),
        "created_at": str(record.created_at),
        "updated_at": str(record.updated_at),
    }


def _temporary_conversation_title(text: str, *, limit: int = 24) -> str:
    value = " ".join(str(text or "").split())
    if not value:
        return "未命名临时会话"
    return value if len(value) <= limit else value[:limit] + "…"


def _temporary_surface_session_id(surface: str) -> str:
    value = str(surface or "").strip()
    if not value.startswith("temporary:"):
        return ""
    return value.partition(":")[2].strip()


def _temporary_command_blocked(text: str) -> bool:
    token = str(text or "").strip().partition(" ")[0].lower()
    return token in {"/new", "/reset", "/delete"}


def _is_main_surface(surface: str) -> bool:
    return surface == "main" or bool(_temporary_surface_session_id(surface))


def _main_message_scheduler(deps: WebDeps) -> ConversationSessionScheduler | None:
    if deps.local_message_scheduler is not None:
        return deps.local_message_scheduler
    if deps.primary_scheduler_handle is not None:
        return deps.primary_scheduler_handle.scheduler
    return None


def _message_scheduler(
    deps: WebDeps,
    surface: str,
) -> ConversationSessionScheduler | None:
    if _is_assistant_surface(deps, surface):
        return deps.assistant_message_scheduler
    return _main_message_scheduler(deps)


async def _submit_enqueued_message_to_scheduler(
    deps: WebDeps,
    result: ConversationSubmitResult,
    scheduler: ConversationSessionScheduler,
    turn: InboundTurn,
) -> None:
    if (
        result.message is not None
        and result.message.append_mode == ConversationAppendMode.APPEND_CURRENT_TURN
    ):
        append_current = getattr(scheduler, "try_append_to_current", None)
        if callable(append_current) and await append_current(result.message.message_id):
            return
    await scheduler.submit(turn)


def _command_scheduler(
    deps: WebDeps,
    surface: str,
    scheduled_main: bool,
) -> ConversationCommandScheduler | None:
    if _is_assistant_surface(deps, surface):
        return None
    if deps.local_command_scheduler is not None:
        return deps.local_command_scheduler
    if scheduled_main and deps.primary_command_handle is not None:
        return deps.primary_command_handle.scheduler
    return None


def _websocket_client_action(data: dict[str, Any]) -> str:
    if str(data.get("type") or "").strip() != "client_action":
        return ""
    return str(data.get("action") or "").strip()


def _websocket_action_payload(data: dict[str, Any]) -> dict[str, Any]:
    action = _websocket_client_action(data)
    payload = dict(data)
    if action == "append_current_turn":
        payload["append_mode"] = ConversationAppendMode.APPEND_CURRENT_TURN.value
        return payload
    if action == "save_draft":
        payload["append_mode"] = ConversationAppendMode.DRAFT.value
        return payload
    if action == "confirm_command":
        payload["text"] = str(data.get("text") or "").strip()
        payload["command_confirmed_while_running"] = True
        payload["append_mode"] = ""
        return payload
    if action == "switch_session":
        payload["text"] = _switch_session_command(data)
        payload["append_mode"] = ""
        return payload
    return payload


def _create_command_confirmation_request(
    deps: WebDeps,
    surface: str,
    result: ConversationSubmitResult,
    turn: InboundTurn,
    reason: str,
) -> Any:
    classification = result.classification
    command_text = str(turn.text or classification.normalized_text or "").strip()
    return deps.conversation_controller.record_command_confirmation_required(
        session_id=result.session_id,
        classification=classification,
        command_text=command_text,
        reason=reason,
        surface=surface,
        source="web",
    )


def _command_requires_running_confirmation(result: ConversationSubmitResult) -> bool:
    return (
        result.classification.command_availability
        == CommandAvailability.CONFIRM_WHILE_RUNNING
    )


async def _resolve_command_confirmation_action(
    deps: WebDeps,
    surface: str,
    data: dict[str, Any],
    send_json: Callable[[dict[str, Any]], Awaitable[None]],
) -> dict[str, Any] | None:
    client_action_id = str(data.get("client_action_id") or "").strip()
    repository = deps.conversation_controller.repository
    request_id = str(data.get("request_id") or "").strip()
    if not request_id:
        await send_json(
            websocket_server_event(
                "ui.error",
                message="缺少确认请求编号。",
                action="confirm_command",
                client_action_id=client_action_id,
                action_result=_action_result_payload(
                    ok=False,
                    action="confirm_command",
                    client_action_id=client_action_id,
                    target_id=request_id,
                    reason="缺少确认请求编号。",
                    code="missing_request_id",
                ),
            )
        )
        await _send_conversation_snapshot(deps, surface, send_json)
        return None
    request = repository.get_input_request(request_id)
    session_id = _surface_session_id(deps, surface)
    if (
        request is None
        or request.session_id != session_id
        or request.kind != ConversationInputRequestKind.CONFIRMATION
    ):
        await send_json(
            websocket_server_event(
                "ui.error",
                message="确认请求不存在或不属于当前会话。",
                action="confirm_command",
                client_action_id=client_action_id,
                action_result=_action_result_payload(
                    ok=False,
                    action="confirm_command",
                    client_action_id=client_action_id,
                    target_id=request_id,
                    reason="确认请求不存在或不属于当前会话。",
                    code="request_not_found",
                ),
            )
        )
        await _send_conversation_snapshot(deps, surface, send_json)
        return None
    if request.status != ConversationInputRequestStatus.PENDING:
        await send_json(
            websocket_server_event(
                "ui.notice",
                message="这个确认请求已经处理过。",
                action="confirm_command",
                client_action_id=client_action_id,
                action_result=_action_result_payload(
                    ok=True,
                    action="confirm_command",
                    client_action_id=client_action_id,
                    target_id=request_id,
                    reason="already_processed",
                ),
            )
        )
        await _send_conversation_snapshot(deps, surface, send_json)
        return None
    decision = str(data.get("decision") or "confirm").strip().lower()
    detail = request.detail if isinstance(request.detail, dict) else {}
    command_text = str(data.get("text") or detail.get("command_text") or "").strip()
    if decision in {"cancel", "cancelled", "deny", "denied", "reject", "rejected"}:
        _record_web_confirmation_input(
            deps,
            surface,
            request=request,
            decision="cancelled",
            command_text=command_text,
            client_action_id=client_action_id,
        )
        await send_json(
            websocket_server_event(
                "ui.notice",
                message="已取消命令。",
                action="confirm_command",
                client_action_id=client_action_id,
                action_result=_action_result_payload(
                    ok=True,
                    action="confirm_command",
                    client_action_id=client_action_id,
                    target_id=request_id,
                    reason="cancelled",
                ),
            )
        )
        await _send_conversation_snapshot(deps, surface, send_json)
        return None
    if not command_text:
        await send_json(
            websocket_server_event(
                "ui.error",
                message="确认请求缺少命令文本。",
                action="confirm_command",
                client_action_id=client_action_id,
                action_result=_action_result_payload(
                    ok=False,
                    action="confirm_command",
                    client_action_id=client_action_id,
                    target_id=request_id,
                    reason="确认请求缺少命令文本。",
                    code="missing_command_text",
                ),
            )
        )
        await _send_conversation_snapshot(deps, surface, send_json)
        return None
    _record_web_confirmation_input(
        deps,
        surface,
        request=request,
        decision="confirmed",
        command_text=command_text,
        client_action_id=client_action_id,
    )
    await send_json(
        _action_success_event(
            surface=surface,
            action="confirm_command",
            client_action_id=client_action_id,
            target_id=request_id,
            result={
                "ok": True,
                "request_id": request_id,
                "decision": "confirmed",
                "command_text": command_text,
            },
        )
    )
    payload = _websocket_action_payload({**data, "text": command_text})
    payload["command_confirmed_while_running"] = True
    return payload


def _input_response_kind_for_request(
    kind: ConversationInputRequestKind,
) -> ConversationInputKind:
    if kind == ConversationInputRequestKind.QUESTION:
        return ConversationInputKind.QUESTION_RESPONSE
    if kind == ConversationInputRequestKind.CONFIRMATION:
        return ConversationInputKind.CONFIRMATION_RESPONSE
    return ConversationInputKind.APPROVAL_RESPONSE


async def _handle_websocket_input_request_response(
    deps: WebDeps,
    surface: str,
    data: dict[str, Any],
    send_json: Callable[[dict[str, Any]], Awaitable[None]],
    *,
    action_name: str = "input_request_response",
) -> None:
    clean_action_name = str(action_name or "input_request_response").strip()
    client_action_id = str(data.get("client_action_id") or "").strip()
    repository = deps.conversation_controller.repository
    request_id = str(data.get("request_id") or "").strip()
    target_id = _action_target_id(data)
    request = repository.get_input_request(request_id)
    session_id = _surface_session_id(deps, surface)
    if (
        request is None
        or request.session_id != session_id
        or _surface_turn_hidden(repository, session_id, request.turn_id)
    ):
        await send_json(
            websocket_server_event(
                "ui.error",
                message="待输入请求不存在或不属于当前会话。",
                action=clean_action_name,
                client_action_id=client_action_id,
                action_result=_action_result_payload(
                    ok=False,
                    action=clean_action_name,
                    client_action_id=client_action_id,
                    target_id=target_id,
                    reason="待输入请求不存在或不属于当前会话。",
                    code="request_not_found",
                ),
            )
        )
        await _send_conversation_snapshot(deps, surface, send_json)
        return
    if request.kind == ConversationInputRequestKind.APPROVAL:
        await send_json(
            websocket_server_event(
                "ui.error",
                message="审批请走审批动作。",
                action=clean_action_name,
                client_action_id=client_action_id,
                action_result=_action_result_payload(
                    ok=False,
                    action=clean_action_name,
                    client_action_id=client_action_id,
                    target_id=target_id,
                    reason="审批请走审批动作。",
                    code="approval_requires_approval_action",
                ),
            )
        )
        await _send_conversation_snapshot(deps, surface, send_json)
        return
    if request.status != ConversationInputRequestStatus.PENDING:
        await send_json(
            websocket_server_event(
                "ui.notice",
                message="这个待输入请求已经处理过。",
                action=clean_action_name,
                client_action_id=client_action_id,
                action_result=_action_result_payload(
                    ok=True,
                    action=clean_action_name,
                    client_action_id=client_action_id,
                    target_id=target_id,
                    reason="already_processed",
                ),
            )
        )
        await _send_conversation_snapshot(deps, surface, send_json)
        return
    decision = str(data.get("decision") or "answered").strip().lower()
    answer = data.get("answer")
    response = data.get("response") if isinstance(data.get("response"), dict) else {}
    if answer is not None:
        response = {**response, "answer": answer}
    selected_options = data.get("selected_options")
    if isinstance(selected_options, list):
        response = {
            **response,
            "selected_options": [str(item) for item in selected_options],
        }
    if not response:
        response = {"decision": decision}
    command_text = ""
    if request.kind == ConversationInputRequestKind.CONFIRMATION:
        detail = request.detail if isinstance(request.detail, dict) else {}
        command_text = str(
            data.get("text") or detail.get("command_text") or ""
        ).strip()
        if command_text:
            response = {**response, "command_text": command_text}
    status = (
        ConversationInputRequestStatus.CANCELLED
        if decision in {"cancel", "cancelled", "deny", "denied", "reject", "rejected"}
        else ConversationInputRequestStatus.RESOLVED
    )
    question_pending_in_runtime = False
    if request.kind == ConversationInputRequestKind.QUESTION:
        question_pending_in_runtime = (
            deps.question_registry is not None
            and request_id in set(deps.question_registry.pending_ids())
        )
    runtime_missing_question = (
        request.kind == ConversationInputRequestKind.QUESTION
        and not question_pending_in_runtime
    )
    recovered_runtime_missing_question = bool(
        runtime_missing_question
        and (request.metadata or {}).get("recovery_runtime_missing")
    )
    request_tool_record_id = str(getattr(request, "tool_record_id", "") or "").strip()
    request_tool_call_id = str(getattr(request, "tool_call_id", "") or "").strip()
    request_turn_id = str(getattr(request, "turn_id", "") or "").strip()
    metadata: dict[str, Any] = {
        "input_kind": _input_response_kind_for_request(request.kind).value,
        "conversation_session_id": session_id,
        "input_request_id": request_id,
        "input_request_kind": request.kind.value,
        "decision": decision,
        "response": response,
        "command_text": command_text,
        "answer": str(response.get("answer") or ""),
        "client_action_id": client_action_id,
        "selected_options": (
            [str(item) for item in selected_options]
            if isinstance(selected_options, list)
            else []
        ),
        "feedback": str(data.get("feedback") or response.get("feedback") or ""),
        "question_runtime_missing": runtime_missing_question,
        "question_recovery_continuation": recovered_runtime_missing_question,
    }
    if request_tool_record_id:
        metadata["tool_record_id"] = request_tool_record_id
    if request_tool_call_id:
        metadata["tool_call_id"] = request_tool_call_id
    if request_turn_id:
        metadata["turn_id"] = request_turn_id
    deps.conversation_controller.submit_input(
        ConversationInput(
            ref=_surface_ref(deps, surface),
            text=str(answer if answer is not None else decision),
            raw_event_id=f"input-request:{request_id}:{decision}",
            source="web",
            surface=surface,
            metadata=metadata,
        )
    )
    if (
        request.kind == ConversationInputRequestKind.QUESTION
        and deps.question_registry is not None
        and question_pending_in_runtime
    ):
        deps.question_registry.resolve(
            request_id,
            decision="cancelled" if status == ConversationInputRequestStatus.CANCELLED else decision,
            answer=str(response.get("answer") or ""),
            selected_options=(
                [str(item) for item in selected_options]
                if isinstance(selected_options, list)
                else []
            ),
            feedback=str(data.get("feedback") or response.get("feedback") or ""),
            response=response,
        )
    continuation_message = None
    if recovered_runtime_missing_question:
        continuation_message = await _resume_recovered_input_request(
            deps,
            surface,
            request,
            kind="question",
            decision=(
                "cancelled"
                if status == ConversationInputRequestStatus.CANCELLED
                else decision
            ),
            response=dict(response),
            feedback=str(data.get("feedback") or response.get("feedback") or ""),
            client_action_id=client_action_id,
        )
    refreshed = repository.get_input_request(request_id) or request
    await send_json(
        websocket_server_event(
            "ui.notice",
            message=(
                "已取消待输入请求。"
                if status == ConversationInputRequestStatus.CANCELLED
                else "已记录回复，正在继续。"
                if runtime_missing_question and continuation_message is not None
                else "回答已记录，但当前运行已不在。请从失败点重试。"
                if runtime_missing_question
                else "已记录回复。"
            ),
            action=clean_action_name,
            client_action_id=client_action_id,
            result={
                "ok": True,
                "request": _input_request_snapshot(refreshed),
                "request_id": request_id,
                "decision": decision,
                "runtime_missing": runtime_missing_question,
                "continuation_message": (
                    _message_snapshot(continuation_message)
                    if continuation_message is not None
                    else None
                ),
                "tool_record_id": str(
                    getattr(refreshed, "tool_record_id", "") or request_tool_record_id
                ),
                "tool_call_id": str(
                    getattr(refreshed, "tool_call_id", "") or request_tool_call_id
                ),
            },
            action_result=_action_result_payload(
                ok=True,
                action=clean_action_name,
                client_action_id=client_action_id,
                target_id=target_id,
                reason=(
                    "cancelled"
                    if status == ConversationInputRequestStatus.CANCELLED
                    else "runtime_missing"
                    if recovered_runtime_missing_question
                    else "recorded"
                ),
            ),
        )
    )
    await _send_conversation_snapshot(deps, surface, send_json)


async def _handle_websocket_queue_action(
    deps: WebDeps,
    surface: str,
    action: str,
    data: dict[str, Any],
    send_json: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    client_action_id = str(data.get("client_action_id") or "").strip()
    target_id = _action_target_id(data)
    try:
        result = await _run_websocket_queue_action(deps, surface, action, data)
    except HTTPException as error:
        await send_json(
            _action_error_event(
                action=action,
                client_action_id=client_action_id,
                target_id=target_id,
                error=error,
            )
        )
        await _send_conversation_snapshot(deps, surface, send_json)
        return
    await send_json(
        _action_success_event(
            surface=surface,
            action=action,
            client_action_id=client_action_id,
            target_id=target_id,
            result=result,
        )
    )
    await _send_conversation_snapshot(deps, surface, send_json)


async def _handle_websocket_approval_cancel(
    deps: WebDeps,
    surface: str,
    data: dict[str, Any],
    send_json: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    client_action_id = str(data.get("client_action_id") or "").strip()
    approval_id = str(
        data.get("approval_id") or data.get("request_id") or ""
    ).strip()
    target_id = _action_target_id(data)
    try:
        result = _cancel_approval_response(
            deps,
            surface,
            approval_id,
            source="web",
            client_action_id=client_action_id,
        )
    except HTTPException as error:
        await send_json(
            _action_error_event(
                action="cancel_approval",
                client_action_id=client_action_id,
                target_id=target_id,
                error=error,
            )
        )
        await _send_conversation_snapshot(deps, surface, send_json)
        return
    await send_json(
        _action_success_event(
            surface=surface,
            action="cancel_approval",
            client_action_id=client_action_id,
            target_id=target_id,
            result=result,
        )
    )
    await _send_conversation_snapshot(deps, surface, send_json)


async def _handle_websocket_approval_response(
    deps: WebDeps,
    surface: str,
    data: dict[str, Any],
    send_json: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    client_action_id = str(data.get("client_action_id") or "").strip()
    request_id = str(data.get("request_id") or "").strip()
    target_id = _action_target_id(data)
    decision = str(data.get("decision") or "deny").strip() or "deny"
    feedback = str(data.get("feedback") or "")
    scope = str(data.get("scope") or "")
    modified_args = (
        data.get("modified_args") if isinstance(data.get("modified_args"), dict) else None
    )
    repository = deps.conversation_controller.repository
    session_id = _surface_session_id(deps, surface)
    persisted_approval = repository.get_approval(request_id)
    if (
        persisted_approval is not None
        and persisted_approval.session_id == session_id
        and _surface_turn_hidden(repository, session_id, persisted_approval.turn_id)
    ):
        await send_json(
            websocket_server_event(
                "ui.error",
                message="审批请求不存在或不属于当前会话。",
                action="approval",
                client_action_id=client_action_id,
                action_result=_action_result_payload(
                    ok=False,
                    action="approval",
                    client_action_id=client_action_id,
                    target_id=target_id,
                    reason="审批请求不存在或不属于当前会话。",
                    code="approval_not_found",
                ),
            )
        )
        await _send_conversation_snapshot(deps, surface, send_json)
        return
    approval_snapshot = _approval_snapshot_for_request(deps, surface, request_id)
    normalized_decision = str(decision or "").strip().lower()
    permanent_approval = (
        normalized_decision in {"approve", "approved", "yes", "1"}
        and scope == "permanent"
        and approval_snapshot is not None
    )
    if permanent_approval:
        _save_permanent_tool_approval(
            deps,
            str(approval_snapshot.get("tool_name") or ""),
        )
    resolved_runtime = False
    if deps.approval_registry is not None:
        resolved_runtime = deps.approval_registry.resolve(
            request_id,
            decision,
            feedback,
            scope="once" if permanent_approval else scope,
            modified_args=modified_args,
        )
        if (
            not resolved_runtime
            and str(decision or "").strip().lower() in {"approve", "approved", "yes", "1"}
            and scope in {"today", "week"}
            and approval_snapshot is not None
        ):
            deps.approval_registry.add_grant(
                tool_name=str(approval_snapshot.get("tool_name", "")),
                action=str(approval_snapshot.get("action", "")),
                scope=scope,
            )
    recovered_request = repository.get_input_request(request_id)
    recovered_runtime_missing = bool(
        recovered_request is not None
        and (recovered_request.metadata or {}).get("recovery_runtime_missing")
    )
    if resolved_runtime or not _is_approval_decision(decision):
        _record_web_approval_input(
            deps,
            surface,
            data,
            runtime_missing=(not resolved_runtime and recovered_runtime_missing),
            runtime_resolved=resolved_runtime,
            runtime_cancelled=(
                resolved_runtime
                and normalized_decision in {"cancel", "cancelled", "canceled"}
            ),
        )
        continuation_message = None
        if not resolved_runtime and recovered_runtime_missing and recovered_request is not None:
            continuation_message = await _resume_recovered_input_request(
                deps,
                surface,
                recovered_request,
                kind="approval",
                decision=normalized_decision,
                response={
                    "decision": normalized_decision,
                    "feedback": feedback,
                    "scope": scope,
                    "modified_args": dict(modified_args or {}),
                },
                feedback=feedback,
                client_action_id=client_action_id,
            )
        await send_json(
            _action_success_event(
                surface=surface,
                action="approval",
                client_action_id=client_action_id,
                target_id=target_id,
                result={
                    "ok": True,
                    "request_id": request_id,
                    "decision": decision,
                    "runtime_resolved": resolved_runtime,
                    "continuation_message": (
                        _message_snapshot(continuation_message)
                        if continuation_message is not None
                        else None
                    ),
                    "approval": approval_snapshot,
                    "tool_record_id": (
                        approval_snapshot.get("tool_record_id")
                        if approval_snapshot is not None
                        else ""
                    ),
                    "tool_call_id": (
                        approval_snapshot.get("tool_call_id")
                        if approval_snapshot is not None
                        else ""
                    ),
                },
            )
        )
        await _send_conversation_snapshot(deps, surface, send_json)
        return

    _record_web_approval_input(
        deps,
        surface,
        data,
        runtime_missing=True,
        recovery_continuation=recovered_runtime_missing,
    )
    approval = repository.get_approval(request_id)
    if (
        approval is None
        or approval.session_id != session_id
        or _surface_turn_hidden(repository, session_id, approval.turn_id)
    ):
        await send_json(
            websocket_server_event(
                "ui.error",
                message="审批请求不存在或不属于当前会话。",
                action="approval",
                client_action_id=client_action_id,
                action_result=_action_result_payload(
                    ok=False,
                    action="approval",
                    client_action_id=client_action_id,
                    target_id=target_id,
                    reason="审批请求不存在或不属于当前会话。",
                    code="approval_not_found",
                ),
            )
        )
        await _send_conversation_snapshot(deps, surface, send_json)
        return
    refreshed = repository.get_approval(request_id)
    if refreshed is None or refreshed.status != ConversationApprovalStatus.APPROVED:
        await send_json(
            websocket_server_event(
                "ui.error",
                message="审批请求已经不在待处理状态。",
                action="approval",
                client_action_id=client_action_id,
                action_result=_action_result_payload(
                    ok=False,
                    action="approval",
                    client_action_id=client_action_id,
                    target_id=target_id,
                    reason="审批请求已经不在待处理状态。",
                    code="approval_not_pending",
                ),
            )
        )
        await _send_conversation_snapshot(deps, surface, send_json)
        return
    continuation_message = None
    if recovered_runtime_missing and recovered_request is not None:
        continuation_message = await _resume_recovered_input_request(
            deps,
            surface,
            recovered_request,
            kind="approval",
            decision=normalized_decision,
            response={
                "decision": normalized_decision,
                "feedback": feedback,
                "scope": scope,
                "modified_args": dict(modified_args or {}),
            },
            feedback=feedback,
            client_action_id=client_action_id,
        )
    await send_json(
        _action_success_event(
            surface=surface,
            action="approval",
            client_action_id=client_action_id,
            target_id=target_id,
            result={
                "ok": True,
                "approval": _approval_snapshot(refreshed or approval),
                "runtime_missing": True,
                "continuation_message": (
                    _message_snapshot(continuation_message)
                    if continuation_message is not None
                    else None
                ),
            },
        )
    )
    await _send_conversation_snapshot(deps, surface, send_json)


async def _handle_websocket_tool_cancel_by_record(
    deps: WebDeps,
    surface: str,
    tool_record_id: str,
    send_json: Callable[[dict[str, Any]], Awaitable[None]],
    *,
    client_action_id: str = "",
) -> None:
    target_id = str(tool_record_id or "").strip()
    try:
        result = _cancel_tool_call_response(
            deps,
            surface,
            tool_record_id,
            record_requested=False,
            source="web",
            client_action_id=client_action_id,
        )
    except HTTPException as error:
        await send_json(
            _action_error_event(
                action="cancel_tool_call",
                client_action_id=client_action_id,
                target_id=target_id,
                error=error,
            )
        )
        await _send_conversation_snapshot(deps, surface, send_json)
        return
    await send_json(
        _action_success_event(
            surface=surface,
            action="cancel_tool_call",
            client_action_id=client_action_id,
            target_id=target_id,
            result=result,
        )
    )
    await _send_conversation_snapshot(deps, surface, send_json)


async def _handle_websocket_tool_cancel(
    deps: WebDeps,
    surface: str,
    data: dict[str, Any],
    send_json: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    client_action_id = str(data.get("client_action_id") or "").strip()
    tool_call_id = str(data.get("tool_call_id") or "").strip()
    tool_record_id = str(data.get("tool_record_id") or "").strip()
    tool_call = _surface_tool_call_for_cancel(
        deps,
        surface,
        tool_record_id=tool_record_id,
        tool_call_id=tool_call_id,
    )
    resolved_record_id = str(
        getattr(tool_call, "tool_record_id", "") or tool_record_id
    ).strip()
    resolved_call_id = str(
        getattr(tool_call, "tool_call_id", "") or tool_call_id
    ).strip()
    _record_web_agent_control_input(
        deps,
        surface,
        data,
        control_action="cancel_tool_call",
        cancel_target=CancelTarget.TOOL_CALL,
        target_id=resolved_record_id or resolved_call_id,
        tool_record_id=resolved_record_id,
        tool_call_id=resolved_call_id,
    )
    if resolved_record_id:
        await _handle_websocket_tool_cancel_by_record(
            deps,
            surface,
            resolved_record_id,
            send_json,
            client_action_id=client_action_id,
        )
        return

    cancelled = False
    if deps.approval_registry is not None and resolved_call_id:
        cancelled = bool(
            deps.approval_registry.cancel_by_tool_call(
                resolved_call_id,
                feedback="tool call cancelled",
            )
        )
    if not cancelled and resolved_call_id:
        cancelled = deps.conversation_controller.repository.cancel_waiting_tool_call(
            session_id=_surface_session_id(deps, surface),
            tool_call_id=resolved_call_id,
        )
    _record_web_stop_result(
        deps,
        session_id=_surface_session_id(deps, surface),
        cancelled=cancelled,
        cancel_target=CancelTarget.TOOL_CALL.value,
        requested_turn_id=str(getattr(tool_call, "turn_id", "") or ""),
        target_id=resolved_record_id or resolved_call_id,
        source="web",
        control_action="cancel_tool_call",
        tool_record_id=resolved_record_id,
        tool_call_id=resolved_call_id,
        client_action_id=client_action_id,
    )
    await send_json(
        websocket_server_event(
            "ui.notice" if cancelled else "ui.error",
            message=(
                "已取消工具调用。"
                if cancelled
                else "没有可取消的工具调用。"
            ),
            action="cancel_tool_call",
            client_action_id=client_action_id,
            action_result=_action_result_payload(
                ok=cancelled,
                action="cancel_tool_call",
                client_action_id=client_action_id,
                target_id=resolved_record_id or resolved_call_id,
                reason=(
                    "cancelled"
                    if cancelled
                    else "没有可取消的工具调用。"
                ),
                code="" if cancelled else "tool_call_not_cancellable",
            ),
        )
    )
    await _send_conversation_snapshot(deps, surface, send_json)


async def _run_websocket_queue_action(
    deps: WebDeps,
    surface: str,
    action: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    message_id = str(data.get("message_id") or "").strip()
    tool_record_id = str(data.get("tool_record_id") or "").strip()
    client_action_id = str(data.get("client_action_id") or "").strip()
    if action == "cancel_message":
        return await _cancel_queued_message_response(
            deps,
            surface,
            message_id,
            client_action_id=client_action_id,
        )
    if action == "edit_message":
        return await _edit_queued_message_response(
            deps,
            surface,
            message_id,
            {
                "text": str(data.get("text") or ""),
                "append_mode": str(data.get("append_mode") or ""),
            },
            client_action_id=client_action_id,
        )
    if action == "set_message_append_mode":
        return await _set_queued_append_mode_response(
            deps,
            surface,
            message_id,
            {
                "append_mode": str(data.get("append_mode") or ""),
            },
            client_action_id=client_action_id,
        )
    if action == "promote_message":
        return await _promote_queued_message_response(deps, surface, message_id)
    if action == "reorder_messages":
        ordered_message_ids = data.get("ordered_message_ids")
        if not isinstance(ordered_message_ids, list):
            raise HTTPException(
                status_code=400, detail="ordered_message_ids is required"
            )
        try:
            expected_revision = int(data.get("expected_queue_revision"))
        except (TypeError, ValueError) as error:
            raise HTTPException(
                status_code=400, detail="expected_queue_revision is required"
            ) from error
        repository = deps.conversation_controller.repository
        session_id = _surface_session_id(deps, surface)
        result = repository.reorder_queued_messages(
            session_id,
            [str(item or "") for item in ordered_message_ids],
            expected_revision=expected_revision,
        )
        if result is None:
            raise HTTPException(
                status_code=409, detail="queue changed; refresh and try again"
            )
        return {"ok": True, "queue": result}
    if action == "resume_message":
        return await _resume_queued_message_response(deps, surface, message_id)
    if action == "retry_message":
        return await _retry_message_response(
            deps,
            surface,
            message_id,
            client_action_id=client_action_id,
        )
    if action == "retry_tool_call":
        return await _retry_tool_call_response(
            deps,
            surface,
            tool_record_id,
            client_action_id=client_action_id,
        )
    if action == "edit_last_message":
        return await _edit_last_message_response(
            deps,
            surface,
            str(data.get("text") or ""),
            client_action_id=client_action_id,
        )
    raise HTTPException(status_code=400, detail="unknown queue action")


async def _send_conversation_snapshot(
    deps: WebDeps,
    surface: str,
    send_json: Callable[[dict[str, Any]], Awaitable[None]],
    *,
    client_id: str = "",
) -> None:
    await send_json(
        websocket_server_event(
            "conversation.snapshot",
            surface=surface,
            snapshot=_conversation_snapshot(deps, surface, client_id=client_id),
        )
    )


def _attach_client_event_ack_to_snapshot(
    deps: WebDeps,
    *,
    surface: str,
    client_id: str,
    message: dict[str, Any],
) -> None:
    clean_client_id = str(client_id or "").strip()
    if not clean_client_id:
        return
    event = message.get("event")
    if not isinstance(event, dict) or event.get("type") != "conversation.snapshot":
        return
    snapshot = event.get("snapshot")
    if not isinstance(snapshot, dict):
        return
    session_id = str(snapshot.get("session_id") or "").strip()
    if not session_id:
        return
    latest_event_seq = max(0, int(snapshot.get("latest_event_seq") or 0))
    snapshot["client_event_ack"] = _client_event_ack_snapshot(
        deps.conversation_controller.repository,
        session_id=session_id,
        surface=surface,
        client_id=clean_client_id,
        latest_event_seq=latest_event_seq,
    )


def _switch_session_command(data: dict[str, Any]) -> str:
    command = str(data.get("command") or "new").strip().lower()
    if command in {"new", "reset"}:
        return f"/{command}"
    if command == "delete":
        return "/delete"
    return ""


async def _watch_conversation_events(
    deps: WebDeps,
    surface: str,
    send_json: Callable[[dict[str, Any]], Awaitable[None]],
    *,
    client_id: str = "",
    poll_seconds: float = 0.2,
) -> None:
    """把会话快照和会话事件推到 WebSocket。

    WebSocket 只负责观察：连接建立或会话切换时发一份 snapshot，之后按
    conversation_events.seq 递增推送。页面断开不会影响后端运行。
    """

    repository = deps.conversation_controller.repository
    current_session_id = ""
    cursor = 0
    while True:
        try:
            session_id = _surface_session_id(deps, surface)
            if session_id != current_session_id:
                snapshot = _conversation_snapshot(deps, surface, client_id=client_id)
                current_session_id = str(snapshot.get("session_id") or session_id)
                latest_seq = int(snapshot.get("latest_event_seq") or 0)
                cursor = _conversation_event_cursor_for_client(
                    repository,
                    session_id=current_session_id,
                    surface=surface,
                    client_id=client_id,
                    fallback_seq=latest_seq,
                )
                await send_json(
                    websocket_server_event(
                        "conversation.snapshot",
                        surface=surface,
                        snapshot=snapshot,
                    )
                )
            session_changed_during_flush = False
            while True:
                if _surface_session_id(deps, surface) != current_session_id:
                    session_changed_during_flush = True
                    break
                page = _conversation_events_page(
                    repository,
                    current_session_id,
                    after_seq=cursor,
                    limit=200,
                )
                events = page["events"]
                for event in events:
                    cursor = max(cursor, int(event.seq))
                    await send_json(
                        websocket_server_event(
                            "conversation.event",
                            surface=surface,
                            event=_conversation_event_snapshot(event),
                        )
                    )
                if not events or not page["has_more"]:
                    break
                await asyncio.sleep(0)
            if session_changed_during_flush:
                await asyncio.sleep(0)
                continue
        except Exception as error:  # noqa: BLE001 - 观察通道失败不应关闭聊天连接。
            await send_json(
                websocket_server_event(
                    "ui.error",
                    message=f"会话事件同步失败：{error}",
                )
            )
            await asyncio.sleep(max(float(poll_seconds), 0.05))
            continue
        await asyncio.sleep(max(float(poll_seconds), 0.05))


def _conversation_event_cursor_for_client(
    repository: Any,
    *,
    session_id: str,
    surface: str,
    client_id: str,
    fallback_seq: int,
) -> int:
    clean_client_id = str(client_id or "").strip()
    clean_session_id = str(session_id or "").strip()
    if not clean_client_id or not clean_session_id:
        return max(0, int(fallback_seq or 0))
    ack = repository.get_client_event_ack(
        session_id=clean_session_id,
        client_id=clean_client_id,
        surface=surface,
    )
    if ack is None:
        return max(0, int(fallback_seq or 0))
    return max(0, min(int(ack.latest_event_seq or 0), int(fallback_seq or 0)))


def _running_command_blocked_reason(
    deps: WebDeps,
    surface: str,
    result: ConversationSubmitResult,
) -> str:
    if result.turn is not None and bool(
        (result.turn.metadata or {}).get("command_confirmed_while_running")
    ):
        return ""
    session_id = result.session_id or _surface_session_id(deps, surface)
    running = bool(
        active_turn_id(
            deps.runtime_view,
            session_id,
            include_child_turns=_is_assistant_surface(deps, surface),
        )
    )
    scheduler = _message_scheduler(deps, surface)
    if scheduler is not None and scheduler.is_session_active(session_id):
        running = True
    command_scheduler = _active_command_scheduler(deps, surface)
    if (
        result.turn is not None
        and command_scheduler is not None
        and command_scheduler.is_turn_active(result.turn)
    ):
        running = True
    return command_blocked_reason(result.classification, running=running)


def _active_command_scheduler(
    deps: WebDeps,
    surface: str,
) -> ConversationCommandScheduler | None:
    if _is_assistant_surface(deps, surface):
        return None
    if deps.local_command_scheduler is not None:
        return deps.local_command_scheduler
    if deps.primary_command_handle is not None:
        return deps.primary_command_handle.scheduler
    return None


def _history_response(
    deps: WebDeps,
    surface: str,
    *,
    before_seq: int,
    limit: int,
    history_filter: str = "all",
) -> dict[str, Any]:
    clean_filter = _history_filter_param(history_filter)
    session_id, session_ids = _history_session_ids(deps, surface)
    page = deps.chat_history_view.page(
        session_ids=session_ids,
        before_seq=before_seq,
        limit=limit,
        history_filter=clean_filter,
    )
    return {
        "session_id": session_id,
        "filter": clean_filter,
        "items": [asdict(i) for i in page.items],
        "next_before_seq": page.next_before_seq,
        "has_more": page.has_more,
    }


def _previous_history_response(
    deps: WebDeps,
    surface: str,
    *,
    before_seq: int,
    limit: int,
) -> dict[str, Any]:
    current_session_id, parent_session_id, session_ids = _previous_history_session_ids(
        deps,
        surface,
    )
    page = deps.chat_history_view.page(
        session_ids=session_ids,
        before_seq=before_seq,
        limit=limit,
        history_filter="all",
    )
    return {
        "session_id": current_session_id,
        "parent_session_id": parent_session_id,
        "items": [asdict(item) for item in page.items],
        "next_before_seq": page.next_before_seq,
        "has_more": page.has_more,
    }


def _conversation_snapshot(
    deps: WebDeps,
    surface: str,
    *,
    client_id: str = "",
) -> dict[str, Any]:
    session = _surface_session(deps, surface)
    session_id = session.id
    repository = deps.conversation_controller.repository
    latest_event_seq = repository.latest_event_seq(session_id)
    messages = [
        message
        for message in repository.list_messages(session_id, limit=200)
        if message.status
        in {
            ConversationMessageStatus.QUEUED,
            ConversationMessageStatus.RUNNING,
            ConversationMessageStatus.WAITING_APPROVAL,
            ConversationMessageStatus.FAILED,
        }
    ]
    all_active_turns = [
        run
        for run in deps.runtime_view.list_active_turns()
        if run.session_id == session_id
    ]
    active_turns = [
        run
        for run in all_active_turns
        if _is_assistant_surface(deps, surface) or not run.parent_turn_id
    ]
    active_turn_ids = {
        str(getattr(run, "turn_id", "") or "")
        for run in all_active_turns
        if str(getattr(run, "turn_id", "") or "")
    }
    pending_approvals = repository.list_approvals(
        session_id,
        statuses={ConversationApprovalStatus.PENDING},
        limit=100,
    )
    pending_input_requests = repository.list_input_requests(
        session_id,
        statuses={ConversationInputRequestStatus.PENDING},
        limit=100,
    )
    session_runs = deps.runtime_view.get_session_runs(session_id, limit=500)
    retained_tool_statuses = {
        ConversationToolCallStatus.PENDING,
        ConversationToolCallStatus.STARTED,
        ConversationToolCallStatus.WAITING_APPROVAL,
        ConversationToolCallStatus.APPROVED,
        ConversationToolCallStatus.DENIED,
        ConversationToolCallStatus.FAILED,
    }
    tool_calls = [
        tool_call
        for tool_call in repository.list_tool_calls(session_id, limit=1000)
        if tool_call.status in retained_tool_statuses
        or str(getattr(tool_call, "turn_id", "") or "") in active_turn_ids
    ][-200:]
    agent_id_by_turn = {
        str(getattr(run, "turn_id", "") or ""): str(getattr(run, "agent_id", "") or "")
        for run in session_runs
        if str(getattr(run, "turn_id", "") or "")
    }
    tool_call_snapshots = [
        _tool_call_snapshot(
            tool_call,
            agent_id=agent_id_by_turn.get(
                str(getattr(tool_call, "turn_id", "") or ""),
                "",
            ),
        )
        for tool_call in tool_calls
    ]
    _, history_session_ids = _history_session_ids(deps, surface)
    timeline_page = deps.chat_history_view.page(
        session_ids=history_session_ids,
        before_seq=0,
        limit=80,
        history_filter="all",
    )
    timeline_items = [asdict(item) for item in timeline_page.items]
    runtime_overlays: list[dict[str, Any]] = []
    snapshot_turn = getattr(deps.runtime_view, "snapshot_turn", None)
    if callable(snapshot_turn):
        for run in active_turns:
            turn_id = str(getattr(run, "turn_id", "") or "")
            live_snapshot = snapshot_turn(turn_id, after_seq=0) if turn_id else None
            if live_snapshot is None:
                continue
            message_id = str(
                (dict(getattr(run, "metadata", {}) or {})).get(
                    "conversation_message_id"
                )
                or ""
            )
            for record in live_snapshot.events:
                if str(record.get("type") or "") not in {
                    "llm.text.delta",
                    "llm.thinking.delta",
                    "llm.message.snapshot",
                }:
                    continue
                runtime_overlays.append(
                    {
                        **record,
                        "turn_id": str(record.get("turn_id") or turn_id),
                        "message_id": str(record.get("message_id") or message_id),
                    }
                )
    return {
        "session_id": session_id,
        "history_reset": _history_reset_snapshot(deps, surface, session),
        "latest_event_seq": latest_event_seq,
        "timeline": {
            "revision": latest_event_seq,
            "items": timeline_items,
            "next_before_seq": timeline_page.next_before_seq,
            "has_more": timeline_page.has_more,
        },
        "runtime_overlays": runtime_overlays,
        "client_event_ack": _client_event_ack_snapshot(
            repository,
            session_id=session_id,
            surface=surface,
            client_id=client_id,
            latest_event_seq=latest_event_seq,
        ),
        "messages": [_message_snapshot(message) for message in messages],
        "queue": _queue_snapshot(
            repository,
            session_id,
            messages,
            failed_retryable=surface == "main" or _is_assistant_surface(deps, surface),
        ),
        "actions": _session_actions(
            active_turns=active_turns,
            pending_approvals=pending_approvals,
            pending_input_requests=pending_input_requests,
            scheduler_active=_surface_scheduler_active(deps, surface, session_id),
            can_edit_last_message=repository.can_edit_last_message(session_id),
        ),
        "input_commands": _input_command_hints(deps, surface=surface),
        "active_turns": [_turn_run_snapshot(run) for run in active_turns],
        "tool_calls": tool_call_snapshots,
        "pending_approvals": [
            _approval_snapshot_for_ui(deps, approval) for approval in pending_approvals
        ],
        "pending_input_requests": [
            _input_request_snapshot(request) for request in pending_input_requests
        ],
        "draft": _latest_draft_snapshot(repository, session_id),
        "recovery": _latest_recovery_snapshot(repository, session_id),
        "tool_snapshot": _tool_snapshot_state(deps, session, surface),
    }


def _input_command_hints(deps: WebDeps, *, surface: str = "main") -> dict[str, Any]:
    classifier = getattr(deps.conversation_controller, "classifier", None)
    registry = getattr(classifier, "command_registry", None) or default_command_registry()
    slash_tokens: list[str] = []
    exact_texts: list[str] = []
    definitions: list[dict[str, Any]] = []
    for definition in registry.list():
        if _temporary_surface_session_id(surface) and definition.name in {
            "new",
            "reset",
            "delete",
        }:
            continue
        definitions.append(_input_command_definition_snapshot(definition))
        slash_tokens.extend(
            str(alias or "").strip().lower()
            for alias in definition.aliases
            if str(alias or "").strip().startswith("/")
        )
        exact_texts.extend(
            str(text or "").strip()
            for text in definition.exact_text
            if str(text or "").strip()
        )
    skill_names = sorted(
        str(name or "").strip().lower()
        for name in getattr(classifier, "skill_names", set()) or set()
        if str(name or "").strip()
    )
    for name in skill_names:
        slash_tokens.append(f"/{name}")
        slash_tokens.append(f"/skill:{name}")
    return {
        "slash_tokens": _dedupe_strings(slash_tokens),
        "exact_text": _dedupe_strings(exact_texts),
        "skill_names": _dedupe_strings(skill_names),
        "definitions": definitions,
    }


def _surface_scheduler_active(deps: WebDeps, surface: str, session_id: str) -> bool:
    scheduler = _message_scheduler(deps, surface)
    if scheduler is not None:
        is_session_active = getattr(scheduler, "is_session_active", None)
        if callable(is_session_active) and bool(is_session_active(session_id)):
            return True
    if _is_assistant_surface(deps, surface):
        return False
    command_scheduler = deps.local_command_scheduler
    if command_scheduler is None and deps.primary_command_handle is not None:
        command_scheduler = deps.primary_command_handle.scheduler
    is_turn_active = getattr(command_scheduler, "is_turn_active", None)
    if not callable(is_turn_active):
        return False
    turn = InboundTurn(
        ref=_surface_ref(deps, surface),
        text="/stop",
        metadata={"conversation_session_id": session_id},
    )
    return bool(is_turn_active(turn))


def _active_agent_turn_id(deps: WebDeps, surface: str, agent_id: str) -> str:
    clean_agent_id = str(agent_id or "").strip()
    if not clean_agent_id:
        return ""
    session_id = _surface_session_id(deps, surface)
    runs = [
        run
        for run in deps.runtime_view.list_active_turns()
        if str(getattr(run, "session_id", "") or "") == session_id
        and str(getattr(run, "agent_id", "") or "") == clean_agent_id
        and str(getattr(run, "turn_id", "") or "")
    ]
    if not runs:
        return ""
    runs.sort(key=lambda run: str(getattr(run, "started_at", "") or ""), reverse=True)
    return str(getattr(runs[0], "turn_id", "") or "")


def _should_cancel_local_turn_control(
    *,
    requested_turn_id: str,
    control_turn_id: str,
) -> bool:
    clean_requested = str(requested_turn_id or "").strip()
    if not clean_requested:
        return True
    return clean_requested == str(control_turn_id or "").strip()


def _cancel_target_cancels_session_schedulers(cancel_target: CancelTarget) -> bool:
    return cancel_target in {
        CancelTarget.CURRENT_FOREGROUND_TREE,
        CancelTarget.ENTIRE_AGENT_TREE,
    }


async def _handle_websocket_agent_cancel(
    deps: WebDeps,
    surface: str,
    data: dict[str, Any],
    send_json: Callable[[dict[str, Any]], Awaitable[None]],
    *,
    cancel_current: Callable[..., Awaitable[tuple[bool, str]]],
) -> None:
    client_action_id = str(data.get("client_action_id") or "").strip()
    agent_id = str(data.get("agent_id") or "").strip()
    target_id = str(data.get("turn_id") or "").strip()
    if not target_id:
        target_id = _active_agent_turn_id(deps, surface, agent_id)
    _record_web_agent_control_input(
        deps,
        surface,
        data,
        control_action="cancel_agent",
        cancel_target=CancelTarget.AGENT,
        target_id=target_id or agent_id,
        requested_turn_id=target_id,
        agent_id=agent_id,
    )
    if not target_id:
        _record_web_stop_result(
            deps,
            session_id=_surface_session_id(deps, surface),
            cancelled=False,
            cancel_target=CancelTarget.AGENT.value,
            target_id=agent_id,
            source="web",
            control_action="cancel_agent",
            agent_id=agent_id,
            client_action_id=client_action_id,
        )
        await send_json(
            _stop_action_event(
                surface=surface,
                action="cancel_agent",
                client_action_id=client_action_id,
                target_id=agent_id,
                cancelled=False,
            )
        )
        return
    cancelled, resolved_target_id = await cancel_current(
        target_id,
        cancel_target=CancelTarget.AGENT,
        control_action="cancel_agent",
        client_action_id=client_action_id,
    )
    cancel_tree = _runtime_cancel_tree_snapshot(
        deps.runtime_view,
        target_id,
        include_background=False,
    )
    await send_json(
        _stop_action_event(
            surface=surface,
            action="cancel_agent",
            client_action_id=client_action_id,
            target_id=resolved_target_id or target_id or agent_id,
            cancelled=cancelled,
            turn_id=target_id,
            cancel_tree=cancel_tree,
        )
    )


def _resolved_stop_target_id(
    *,
    cancelled: bool,
    session_id: str,
    requested_turn_id: str = "",
    resolved_turn_id: str = "",
) -> str:
    target_id = str(resolved_turn_id or requested_turn_id or "").strip()
    if target_id:
        return target_id
    clean_session_id = str(session_id or "").strip()
    if cancelled and clean_session_id:
        return f"session:{clean_session_id}"
    return ""


def _conversation_events_response(
    deps: WebDeps,
    surface: str,
    *,
    after_seq: int,
    limit: int,
) -> dict[str, Any]:
    session_id = _surface_session_id(deps, surface)
    repository = deps.conversation_controller.repository
    page = _conversation_events_page(
        repository,
        session_id,
        after_seq=after_seq,
        limit=limit,
    )
    return {
        "session_id": page["session_id"],
        "latest_event_seq": page["latest_event_seq"],
        "next_after_seq": page["next_after_seq"],
        "has_more": page["has_more"],
        "events": [_conversation_event_snapshot(event) for event in page["events"]],
    }


def _tool_snapshot_state(deps: WebDeps, session: Any, surface: str = "main") -> dict[str, Any]:
    stored = session_tool_schema_names(session, session.metadata.root_agent_id)
    # 工具陈旧检测只对「工具来自 settings.tools.enabled」的主 agent 有意义。助手等其它
    # surface 由独立服务配置工具集，与主配置本就不同；拿主配置去比助手会话的快照会把
    # 差集恒判为缺失 → 永远 stale → 前端永远提示「需重新加载」。
    if not _is_main_surface(surface):
        return {
            "has_snapshot": stored is not None,
            "active_tools": list(stored or []),
            "configured_exact_tools": [],
            "missing_configured_tools": [],
            "stale": False,
            "requires_reload": False,
        }
    configured = _configured_exact_tool_names(deps)
    stored_set = set(stored or [])
    missing = [name for name in configured if name not in stored_set]
    stale = stored is not None and bool(missing)
    return {
        "has_snapshot": stored is not None,
        "active_tools": list(stored or []),
        "configured_exact_tools": configured,
        "missing_configured_tools": missing,
        "stale": stale,
        "requires_reload": stale,
    }


def _configured_exact_tool_names(deps: WebDeps) -> list[str]:
    settings = getattr(deps, "settings", None)
    tools = getattr(settings, "tools", None)
    enabled = getattr(tools, "enabled", []) or []
    names: list[str] = []
    seen: set[str] = set()
    for item in enabled:
        name = str(item or "").strip()
        if not name or name == "*" or name.endswith("*"):
            continue
        if name in seen:
            continue
        names.append(name)
        seen.add(name)
    return names


async def _wake_runnable_session_if_needed(
    deps: WebDeps,
    surface: str,
    session_id: str,
) -> bool:
    repository = deps.conversation_controller.repository
    queued = repository.list_queued_messages(
        session_id=session_id,
        runnable_only=True,
        limit=1,
    )
    if not queued:
        return False
    scheduler = _message_scheduler(deps, surface)
    if scheduler is None:
        return False
    await scheduler.ensure_worker(session_id)
    return True


def _message_requires_explicit_resume(message: Any | None) -> bool:
    metadata = getattr(message, "metadata", None) or {}
    return bool(metadata.get("requires_explicit_resume"))


async def _cancel_queued_message_response(
    deps: WebDeps,
    surface: str,
    message_id: str,
    *,
    client_action_id: str = "",
) -> dict[str, Any]:
    repository = deps.conversation_controller.repository
    target = _current_surface_message(deps, surface, message_id)
    _record_web_stop_requested(
        deps,
        session_id=target.session_id,
        cancel_target=CancelTarget.MESSAGE.value,
        target_id=target.message_id,
        source="web",
        control_action="cancel_message",
        client_action_id=client_action_id,
    )
    if not repository.cancel_queued_message(message_id):
        _record_web_stop_result(
            deps,
            session_id=target.session_id,
            cancelled=False,
            cancel_target=CancelTarget.MESSAGE.value,
            target_id=target.message_id,
            source="web",
            control_action="cancel_message",
            client_action_id=client_action_id,
        )
        raise HTTPException(status_code=409, detail="message is not queued")
    _record_web_stop_result(
        deps,
        session_id=target.session_id,
        cancelled=True,
        cancel_target=CancelTarget.MESSAGE.value,
        target_id=target.message_id,
        source="web",
        control_action="cancel_message",
        client_action_id=client_action_id,
    )
    await _wake_runnable_session_if_needed(deps, surface, target.session_id)
    message = repository.get_message(message_id)
    return {"ok": True, "message": _message_snapshot(message)}


async def _edit_queued_message_response(
    deps: WebDeps,
    surface: str,
    message_id: str,
    payload: Any,
    *,
    client_action_id: str = "",
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid payload")
    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="message text is required")
    append_mode = _append_mode_from_payload(payload)
    _current_surface_message(deps, surface, message_id)
    message = deps.conversation_controller.repository.edit_queued_message(
        message_id,
        text=text,
        append_mode=append_mode,
    )
    if message is None:
        raise HTTPException(status_code=409, detail="message is not queued")
    message = await _apply_queued_append_mode_semantics(
        deps,
        surface,
        message,
        client_action_id=client_action_id,
    )
    return {"ok": True, "message": _message_snapshot(message)}


async def _set_queued_append_mode_response(
    deps: WebDeps,
    surface: str,
    message_id: str,
    payload: Any,
    *,
    client_action_id: str = "",
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid payload")
    append_mode = _append_mode_from_payload(payload)
    if append_mode is None or append_mode == ConversationAppendMode.DRAFT:
        raise HTTPException(status_code=400, detail="invalid append_mode")
    _current_surface_message(deps, surface, message_id)
    message = deps.conversation_controller.repository.set_queued_message_append_mode(
        message_id,
        append_mode=append_mode,
    )
    if message is None:
        raise HTTPException(status_code=409, detail="message is not queued")
    message = await _apply_queued_append_mode_semantics(
        deps,
        surface,
        message,
        client_action_id=client_action_id,
    )
    return {"ok": True, "message": _message_snapshot(message)}


async def _apply_queued_append_mode_semantics(
    deps: WebDeps,
    surface: str,
    message: Any,
    *,
    client_action_id: str = "",
) -> Any:
    append_mode = getattr(message, "append_mode", ConversationAppendMode.NEXT_TURN)
    if append_mode == ConversationAppendMode.APPEND_CURRENT_TURN:
        if bool((message.metadata or {}).get("requires_explicit_resume")):
            resumed = deps.conversation_controller.repository.resume_queued_message(
                message.message_id
            )
            if resumed is not None:
                message = resumed
        appended = False
        scheduler = _message_scheduler(deps, surface)
        if scheduler is not None:
            append_current = getattr(scheduler, "try_append_to_current", None)
            if callable(append_current):
                appended = bool(await append_current(message.message_id))
                refreshed = deps.conversation_controller.repository.get_message(
                    message.message_id
                )
                if refreshed is not None:
                    message = refreshed
        if scheduler is not None and not appended:
            message = _cancel_unattached_append_current_message(
                deps,
                message.message_id,
                reason="no_active_turn",
            )
    else:
        await _wake_runnable_session_if_needed(deps, surface, message.session_id)
    return message


def _cancel_unattached_append_current_message(
    deps: WebDeps,
    message_id: str,
    *,
    reason: str,
) -> Any:
    repository = deps.conversation_controller.repository
    message = repository.get_message(message_id)
    if (
        message is not None
        and message.status == ConversationMessageStatus.QUEUED
        and message.append_mode == ConversationAppendMode.APPEND_CURRENT_TURN
    ):
        repository.finalize_appended_message(
            message.message_id,
            ConversationMessageStatus.CANCELLED,
            reason=reason,
        )
        refreshed = repository.get_message(message.message_id)
        if refreshed is not None:
            return refreshed
    return message


async def _promote_queued_message_response(
    deps: WebDeps,
    surface: str,
    message_id: str,
) -> dict[str, Any]:
    repository = deps.conversation_controller.repository
    _current_surface_message(deps, surface, message_id)
    if not repository.promote_queued_message(message_id):
        raise HTTPException(status_code=409, detail="message is not queued")
    message = repository.get_message(message_id)
    if message is not None and not _message_requires_explicit_resume(message):
        scheduler = _message_scheduler(deps, surface)
        if scheduler is not None:
            await scheduler.ensure_worker(message.session_id)
    return {"ok": True, "message": _message_snapshot(message)}


async def _resume_queued_message_response(
    deps: WebDeps,
    surface: str,
    message_id: str,
) -> dict[str, Any]:
    repository = deps.conversation_controller.repository
    current = _current_surface_message(deps, surface, message_id)
    if current.status != ConversationMessageStatus.QUEUED:
        raise HTTPException(status_code=409, detail="message is not queued")
    if not bool((current.metadata or {}).get("requires_explicit_resume")):
        raise HTTPException(status_code=409, detail="message does not require resume")
    scheduler = _message_scheduler(deps, surface)
    if scheduler is None:
        raise HTTPException(status_code=409, detail="message scheduler unavailable")
    message = repository.resume_queued_message(message_id)
    if message is None:
        raise HTTPException(status_code=409, detail="message does not require resume")
    await scheduler.ensure_worker(message.session_id)
    refreshed = repository.get_message(message_id)
    return {"ok": True, "message": _message_snapshot(refreshed or message)}


async def _retry_message_response(
    deps: WebDeps,
    surface: str,
    message_id: str,
    *,
    client_action_id: str = "",
) -> dict[str, Any]:
    repository = deps.conversation_controller.repository
    original = _current_surface_message(deps, surface, message_id)
    if original.status not in {
        ConversationMessageStatus.COMPLETED,
        ConversationMessageStatus.CANCELLED,
        ConversationMessageStatus.FAILED,
    }:
        raise HTTPException(status_code=409, detail="message is not terminal")
    scheduler = _message_scheduler(deps, surface)
    if scheduler is None:
        raise HTTPException(status_code=409, detail="message scheduler unavailable")
    retry_source = "message_retry"
    retry_metadata = _retry_action_metadata(client_action_id)
    checkpoint = _latest_recorded_tool_result_checkpoint(repository, original)
    if checkpoint is not None:
        retry_source = "tool_result"
        retry_metadata.update(
            {
                "retry_after_turn_id": str(original.turn_id or ""),
                "retry_after_tool_record_id": checkpoint.tool_record_id,
                "retry_after_tool_call_id": checkpoint.tool_call_id,
                "retry_after_tool_name": checkpoint.tool_name,
            }
        )
    message = repository.create_retry_message(
        message_id,
        retry_source=retry_source,
        metadata_extra=retry_metadata,
    )
    if message is None:
        raise HTTPException(status_code=409, detail="message cannot be retried")
    repository.promote_queued_message(message.message_id)
    message = repository.get_message(message.message_id) or message
    resume_after_failure = getattr(scheduler, "resume_after_failure", None)
    if callable(resume_after_failure):
        await resume_after_failure(message.session_id)
    else:
        await scheduler.ensure_worker(message.session_id)
    return {"ok": True, "message": _message_snapshot(message)}


async def _resume_recovered_input_request(
    deps: WebDeps,
    surface: str,
    request: Any,
    *,
    kind: str,
    decision: str,
    response: dict[str, Any],
    feedback: str = "",
    client_action_id: str = "",
) -> Any | None:
    repository = deps.conversation_controller.repository
    session_id = str(getattr(request, "session_id", "") or "").strip()
    turn_id = str(getattr(request, "turn_id", "") or "").strip()
    original = repository.get_message_for_turn(session_id, turn_id)
    tool_record_id = str(getattr(request, "tool_record_id", "") or "").strip()
    tool_call = (
        repository.get_tool_call(tool_record_id=tool_record_id)
        if tool_record_id
        else None
    )
    if original is None:
        return None
    if tool_call is None:
        repository.update_message_status(
            original.message_id,
            ConversationMessageStatus.FAILED,
            metadata={
                "error": "restart_interrupted",
                "error_type": "RestartInterrupted",
                "queue_paused_on_failure": True,
                "recovery_continuation_error": "tool_call_not_found",
            },
        )
        return None
    metadata = {
        **_retry_action_metadata(client_action_id),
        "retry_after_turn_id": turn_id,
        "recovery_continuation_kind": str(kind or ""),
        "recovery_request_id": str(getattr(request, "request_id", "") or ""),
        "recovery_decision": str(decision or ""),
        "recovery_response": dict(response or {}),
        "recovery_feedback": str(feedback or ""),
        "recovery_tool_record_id": tool_call.tool_record_id,
        "recovery_tool_call_id": tool_call.tool_call_id,
        "recovery_tool_name": tool_call.tool_name,
        "recovery_tool_args": dict(tool_call.args or {}),
        "recovery_tool_step": int(tool_call.step or 1),
        "recovery_tool_status": str(
            getattr(tool_call.status, "value", tool_call.status) or ""
        ),
    }
    message = repository.create_retry_message(
        original.message_id,
        retry_source="recovery_continuation",
        metadata_extra=metadata,
    )
    if message is None:
        repository.update_message_status(
            original.message_id,
            ConversationMessageStatus.FAILED,
            metadata={
                "error": "restart_interrupted",
                "error_type": "RestartInterrupted",
                "queue_paused_on_failure": True,
                "recovery_continuation_error": "message_creation_failed",
            },
        )
        return None
    repository.update_message_status(
        original.message_id,
        ConversationMessageStatus.COMPLETED,
        metadata={
            "recovery_continued_by_message_id": message.message_id,
            "recovery_continuation_kind": str(kind or ""),
        },
    )
    repository.promote_queued_message(message.message_id)
    message = repository.get_message(message.message_id) or message
    scheduler = _message_scheduler(deps, surface)
    if scheduler is None:
        return message
    resume_after_failure = getattr(scheduler, "resume_after_failure", None)
    if callable(resume_after_failure):
        await resume_after_failure(message.session_id)
    else:
        await scheduler.ensure_worker(message.session_id)
    return message


def _latest_recorded_tool_result_checkpoint(
    repository: Any,
    message: Any,
) -> Any | None:
    """返回失败轮次最后一个已经交给模型的工具结果。"""

    turn_id = str(getattr(message, "turn_id", "") or "").strip()
    session_id = str(getattr(message, "session_id", "") or "").strip()
    if not turn_id or not session_id:
        return None
    tool_calls = repository.list_tool_calls(session_id, turn_id=turn_id, limit=1000)
    if not tool_calls:
        return None
    tools_by_record_id = {item.tool_record_id: item for item in tool_calls}
    tools_by_call_id = {item.tool_call_id: item for item in tool_calls}
    before_seq = 0
    page_size = 500
    while True:
        events = repository.list_events_page(
            session_id,
            before_seq=before_seq,
            limit=page_size,
        )
        if not events:
            return None
        for event in reversed(events):
            if (
                event.turn_id != turn_id
                or event.type != "conversation.tool_call.result"
            ):
                continue
            payload = event.payload or {}
            tool = tools_by_record_id.get(str(payload.get("tool_record_id") or ""))
            if tool is None:
                tool = tools_by_call_id.get(str(payload.get("tool_call_id") or ""))
            if tool is not None:
                return tool
        if len(events) < page_size:
            return None
        before_seq = min(int(event.seq) for event in events)


async def _edit_last_message_response(
    deps: WebDeps,
    surface: str,
    text: str,
    *,
    client_action_id: str = "",
) -> dict[str, Any]:
    repository = deps.conversation_controller.repository
    session_id, _ = _history_session_ids(deps, surface)
    scheduler = _message_scheduler(deps, surface)
    if scheduler is None:
        raise HTTPException(status_code=409, detail="message scheduler unavailable")
    message = repository.edit_and_redo_last_message(
        session_id,
        text=text,
    )
    if message is None:
        raise HTTPException(status_code=409, detail="no last message to edit")
    _invalidate_recaps_after_edit(deps, message)
    del client_action_id
    await scheduler.ensure_worker(message.session_id)
    return {"ok": True, "message": _message_snapshot(message)}


def _invalidate_recaps_after_edit(deps: WebDeps, message: Any) -> None:
    settings = deps.settings
    metadata = dict(getattr(message, "metadata", {}) or {})
    turn_ids = {
        str(turn_id or "").strip()
        for turn_id in list(metadata.get("redo_exclude_turn_ids") or [])
        if str(turn_id or "").strip()
    }
    if settings is None or not turn_ids:
        return
    session_id = str(getattr(message, "session_id", "") or "").strip()
    if not session_id:
        return
    context_repo = ContextMessageRepository.for_memory_space(
        settings.paths.memory_space_dir
    )
    recap_repo = RecapRepository.for_memory_space(settings.paths.memory_space_dir)
    try:
        context_messages = context_repo.list_messages(session_id=session_id)
        recaps = recap_repo.list(session_id=session_id)
        invalid = recaps_covering_turn_ids(recaps, context_messages, turn_ids)
        if not invalid:
            return
        invalid_ids = {recap.recap_id for recap in invalid}
        for recap_id in invalid_ids:
            recap_repo.delete(recap_id)
        remaining = [recap for recap in recaps if recap.recap_id not in invalid_ids]
        latest = max(remaining, key=lambda recap: recap.source_seq_end, default=None)
        RuntimeStateStore.for_agent_dir(
            settings.paths.agent_dir
        ).update_background_session_cursor(
            "recap",
            session_id,
            {
                "last_recap_id": latest.recap_id if latest is not None else "",
                "last_seq": latest.source_seq_end if latest is not None else 0,
            },
        )
    finally:
        context_repo.close()
        recap_repo.close()


def _retry_action_metadata(client_action_id: str) -> dict[str, str]:
    clean_client_action_id = str(client_action_id or "").strip()
    if not clean_client_action_id:
        return {}
    return {"client_action_id": clean_client_action_id}


def _cancel_tool_call_response(
    deps: WebDeps,
    surface: str,
    tool_record_id: str,
    *,
    record_requested: bool = True,
    source: str = "web-api",
    client_action_id: str = "",
) -> dict[str, Any]:
    repository = deps.conversation_controller.repository
    tool_call = _current_surface_tool_call(deps, surface, tool_record_id)
    if record_requested:
        _record_web_stop_requested(
            deps,
            session_id=tool_call.session_id,
            cancel_target=CancelTarget.TOOL_CALL.value,
            requested_turn_id=tool_call.turn_id,
            target_id=tool_call.tool_record_id,
            source=source,
            control_action="cancel_tool_call",
            tool_record_id=tool_call.tool_record_id,
            tool_call_id=tool_call.tool_call_id,
            client_action_id=client_action_id,
        )
    cancelled = False
    if deps.approval_registry is not None:
        cancelled = bool(
            deps.approval_registry.cancel_by_tool_call(
                tool_call.tool_call_id,
                feedback="tool call cancelled",
            )
        )
    if not cancelled:
        cancelled = repository.cancel_waiting_tool_call(
            tool_record_id=tool_call.tool_record_id,
            reason="tool_call_cancelled",
        )
    if not cancelled:
        cancel_tool_call = getattr(deps.runtime_view, "cancel_tool_call", None)
        if callable(cancel_tool_call):
            cancelled_runtime = bool(
                cancel_tool_call(
                    tool_call.tool_call_id,
                    turn_id=tool_call.turn_id,
                )
            )
            if cancelled_runtime:
                repository.cancel_active_tool_call(
                    tool_record_id=tool_call.tool_record_id,
                    reason="tool_call_cancelled",
                )
                cancelled = True
    if not cancelled:
        _record_web_stop_result(
            deps,
            session_id=tool_call.session_id,
            cancelled=False,
            cancel_target=CancelTarget.TOOL_CALL.value,
            requested_turn_id=tool_call.turn_id,
            target_id=tool_call.tool_record_id,
            source=source,
            control_action="cancel_tool_call",
            tool_record_id=tool_call.tool_record_id,
            tool_call_id=tool_call.tool_call_id,
            client_action_id=client_action_id,
        )
        raise HTTPException(
            status_code=409,
            detail="tool call is not waiting for approval or running",
        )
    _record_web_stop_result(
        deps,
        session_id=tool_call.session_id,
        cancelled=True,
        cancel_target=CancelTarget.TOOL_CALL.value,
        requested_turn_id=tool_call.turn_id,
        target_id=tool_call.tool_record_id,
        source=source,
        control_action="cancel_tool_call",
        tool_record_id=tool_call.tool_record_id,
        tool_call_id=tool_call.tool_call_id,
        client_action_id=client_action_id,
    )
    refreshed = repository.get_tool_call(tool_record_id=tool_call.tool_record_id)
    return {"ok": True, "tool_call": _tool_call_snapshot(refreshed or tool_call)}


def _cancel_pending_approvals_by_stop(
    deps: WebDeps,
    surface: str,
    *,
    turn_ids: list[str],
    session_id: str,
    feedback: str = "cancelled by stop",
) -> int:
    registry = deps.approval_registry
    if registry is None:
        return 0
    repository = deps.conversation_controller.repository
    target_turn_ids = {str(turn_id or "").strip() for turn_id in turn_ids}
    target_turn_ids.discard("")
    if not target_turn_ids:
        return 0
    cancelled = 0
    seen: set[str] = set()
    for entry in registry.pending_entries():
        if entry.turn_id not in target_turn_ids:
            continue
        if entry.request_id in seen:
            continue
        seen.add(entry.request_id)
        approval = repository.get_approval(entry.request_id)
        request = repository.get_input_request(entry.request_id)
        request_session_id = str(
            (approval.session_id if approval is not None else "")
            or (request.session_id if request is not None else "")
            or session_id
        ).strip()
        if request_session_id and request_session_id != session_id:
            continue
        deps.conversation_controller.submit_input(
            ConversationInput(
                ref=_surface_ref(deps, surface),
                text="cancelled",
                raw_event_id=f"approval-stop:{entry.request_id}:cancelled",
                source="web",
                surface=surface,
                metadata={
                    "input_kind": ConversationInputKind.APPROVAL_RESPONSE.value,
                    "conversation_session_id": request_session_id or session_id,
                    "approval_request_id": entry.request_id,
                    "tool_call_id": entry.tool_call_id,
                    "decision": "cancelled",
                    "feedback": str(feedback or ""),
                    "approval_runtime_cancelled": True,
                    "stop_cascade": True,
                },
            )
        )
        if registry.resolve(entry.request_id, "cancelled", feedback):
            cancelled += 1
    return cancelled


def _cancel_approval_response(
    deps: WebDeps,
    surface: str,
    approval_id: str,
    *,
    source: str = "web-api",
    client_action_id: str = "",
) -> dict[str, Any]:
    repository = deps.conversation_controller.repository
    clean_approval_id = str(approval_id or "").strip()
    approval = repository.get_approval(clean_approval_id)
    session_id = _surface_session_id(deps, surface)
    if (
        approval is None
        or approval.session_id != session_id
        or _surface_turn_hidden(repository, session_id, approval.turn_id)
    ):
        raise HTTPException(status_code=404, detail="approval request not found")
    registry_cancelled = False
    if deps.approval_registry is not None:
        registry_cancelled = bool(
            deps.approval_registry.cancel_by_request_id(
                approval.approval_id,
                feedback="approval cancelled",
            )
        )
    _record_web_approval_input(
        deps,
        surface,
        {
            "request_id": approval.approval_id,
            "decision": "cancelled",
            "feedback": "approval_cancelled",
            "source": source,
            "client_action_id": client_action_id,
        },
        runtime_cancelled=registry_cancelled,
    )
    refreshed = repository.get_approval(approval.approval_id)
    if refreshed is None or refreshed.status != ConversationApprovalStatus.CANCELLED:
        raise HTTPException(status_code=409, detail="approval request is not pending")
    return {"ok": True, "approval": _approval_snapshot(refreshed or approval)}


async def _retry_tool_call_response(
    deps: WebDeps,
    surface: str,
    tool_record_id: str,
    *,
    client_action_id: str = "",
) -> dict[str, Any]:
    repository = deps.conversation_controller.repository
    tool_call = _current_surface_tool_call(deps, surface, tool_record_id)
    if tool_call.status != ConversationToolCallStatus.FAILED:
        raise HTTPException(status_code=409, detail="tool call is not failed")
    if not tool_call.retryable:
        raise HTTPException(status_code=409, detail="tool call is not retryable")
    scheduler = _message_scheduler(deps, surface)
    if scheduler is None:
        raise HTTPException(status_code=409, detail="message scheduler unavailable")
    message = repository.create_tool_retry_message(
        tool_call.tool_record_id,
        retry_source="tool_failure",
        metadata_extra=_retry_action_metadata(client_action_id),
    )
    if message is None:
        raise HTTPException(status_code=409, detail="tool call cannot be retried")
    await scheduler.ensure_worker(message.session_id)
    refreshed = repository.get_tool_call(tool_record_id=tool_call.tool_record_id)
    return {
        "ok": True,
        "message": _message_snapshot(message),
        "tool_call": _tool_call_snapshot(refreshed or tool_call),
    }


def _append_mode_from_payload(payload: dict[str, Any]) -> ConversationAppendMode | None:
    raw = str(payload.get("append_mode") or "").strip()
    if not raw:
        return None
    try:
        return ConversationAppendMode(raw)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="invalid append_mode") from error


def _current_surface_message(
    deps: WebDeps,
    surface: str,
    message_id: str,
) -> Any:
    message = deps.conversation_controller.repository.get_message(message_id)
    if message is None or message.session_id != _surface_session_id(deps, surface):
        raise HTTPException(status_code=404, detail="message not found")
    return message


def _current_surface_tool_call(
    deps: WebDeps,
    surface: str,
    tool_record_id: str,
) -> Any:
    repository = deps.conversation_controller.repository
    tool_call = repository.get_tool_call(
        tool_record_id=tool_record_id
    )
    session_id = _surface_session_id(deps, surface)
    if (
        tool_call is None
        or tool_call.session_id != session_id
        or _surface_turn_hidden(repository, session_id, tool_call.turn_id)
    ):
        raise HTTPException(status_code=404, detail="tool call not found")
    return tool_call


def _surface_tool_call_for_cancel(
    deps: WebDeps,
    surface: str,
    *,
    tool_record_id: str = "",
    tool_call_id: str = "",
) -> Any | None:
    repository = deps.conversation_controller.repository
    session_id = _surface_session_id(deps, surface)
    clean_record_id = str(tool_record_id or "").strip()
    if clean_record_id:
        tool_call = repository.get_tool_call(tool_record_id=clean_record_id)
        if (
            tool_call is not None
            and tool_call.session_id == session_id
            and not _surface_turn_hidden(repository, session_id, tool_call.turn_id)
        ):
            return tool_call
    clean_call_id = str(tool_call_id or "").strip()
    if clean_call_id:
        tool_call = repository.get_tool_call(
            session_id=session_id,
            tool_call_id=clean_call_id,
        )
        if tool_call is not None and not _surface_turn_hidden(
            repository,
            session_id,
            tool_call.turn_id,
        ):
            return tool_call
    return None


def _surface_turn_hidden(repository: Any, session_id: str, turn_id: str) -> bool:
    # 撤回功能已移除：不存在被隐藏的轮次。
    del repository, session_id, turn_id
    return False


def _history_session_ids(deps: WebDeps, surface: str) -> tuple[str, list[str]]:
    temporary_session_id = _temporary_surface_session_id(surface)
    if temporary_session_id:
        session = _surface_session(deps, surface)
        return session.id, [session.id]
    if _is_assistant_surface(deps, surface):
        service = deps.assistant_service
        session = service.sessions_for(surface).active_session(
            service.session_manager, _assistant_ref(surface)
        )
        return session.id, [session.id]

    current = deps.session_manager.get_or_create(deps.main_ref)
    return current.id, [current.id]


def _previous_history_session_ids(
    deps: WebDeps,
    surface: str,
) -> tuple[str, str, list[str]]:
    current = _surface_session(deps, surface)
    parent_session_id = str(current.metadata.parent_session_id or "").strip()
    if not parent_session_id or _temporary_surface_session_id(surface):
        return current.id, "", []
    manager = (
        deps.assistant_service.session_manager
        if _is_assistant_surface(deps, surface)
        else deps.session_manager
    )
    session_ids: list[str] = []
    seen = {current.id}
    next_session_id = parent_session_id
    while next_session_id and next_session_id not in seen:
        seen.add(next_session_id)
        previous = manager.load(next_session_id)
        if previous is None:
            break
        session_ids.append(previous.id)
        next_session_id = str(previous.metadata.parent_session_id or "").strip()
    session_ids.reverse()
    return current.id, parent_session_id, session_ids


def _history_reset_snapshot(deps: WebDeps, surface: str, session: Any) -> dict[str, Any] | None:
    if _temporary_surface_session_id(surface):
        return None
    parent_session_id = str(session.metadata.parent_session_id or "").strip()
    if not parent_session_id:
        return None
    _, _, session_ids = _previous_history_session_ids(deps, surface)
    return {
        "parent_session_id": parent_session_id,
        "available": bool(session_ids),
    }


async def _run_surface_turn(
    deps: WebDeps,
    surface: str,
    turn: InboundTurn,
    send_json: Callable[[dict[str, Any]], Awaitable[None]],
    *,
    control: TurnStreamControl,
    abort_check: CancelHandle,
) -> None:
    if _is_assistant_surface(deps, surface):
        service = deps.assistant_service
        ref = _assistant_ref(surface)
        session_id = service.sessions_for(surface).active_session(
            service.session_manager, ref
        ).id
        entry = lambda: service.handle_user_message(  # noqa: E731
            assistant_id=surface,
            ref=ref,
            text=turn.text,
            approval_channel=deps.approval_channel,
            question_channel=deps.question_channel,
            abort_check=abort_check,
        )
        extract_final = _assistant_final
    else:
        ref = deps.main_ref
        session_id = _surface_session_id(deps, surface)
        entry = lambda: deps.coordinator.handle_turn(  # noqa: E731
            turn,
            abort_check=abort_check,
        )
        extract_final = _main_final

    repository = deps.conversation_controller.repository
    update_conversation_turn_status(
        repository,
        turn,
        ConversationMessageStatus.RUNNING,
    )
    turn_result = await run_send_path_turn(
        entry=entry,
        session_id=session_id,
        runtime_view=deps.runtime_view,
        send_json=send_json,
        extract_final=extract_final,
        control=control,
    )
    if turn_result is None:
        _record_surface_command_result(
            repository,
            turn,
            status="failed",
            turn_id="",
            response="",
        )
        update_conversation_turn_status(
            repository,
            turn,
            ConversationMessageStatus.FAILED,
        )
        return
    turn_id, _text, aborted = turn_result
    _record_surface_command_result(
        repository,
        turn,
        status="cancelled" if aborted else "completed",
        turn_id=turn_id,
        response=_text,
    )
    status = (
        ConversationMessageStatus.CANCELLED
        if aborted
        else ConversationMessageStatus.COMPLETED
    )
    update_conversation_turn_status(repository, turn, status, turn_id=turn_id)


def _record_surface_command_result(
    repository: Any,
    turn: InboundTurn,
    *,
    status: str,
    turn_id: str,
    response: str,
) -> None:
    record_conversation_command_result(
        repository,
        turn,
        status=status,
        turn_id=turn_id,
        response=response,
    )


def _surface_ref(deps: WebDeps, surface: str) -> ConversationRef:
    if _is_assistant_surface(deps, surface):
        return _assistant_ref(surface)
    return deps.main_ref


def _conversation_input_from_web_data(
    deps: WebDeps,
    surface: str,
    data: dict[str, Any],
) -> ConversationInput:
    text = str(data.get("text") or "").strip()
    client_action_id = str(data.get("client_action_id") or "").strip()
    raw_event_id = str(
        data.get("raw_event_id") or client_action_id or data.get("client_id") or ""
    ).strip()
    idempotency_key = str(data.get("idempotency_key") or raw_event_id or "").strip()
    return ConversationInput(
        ref=_surface_ref(deps, surface),
        text=text,
        raw_event_id=raw_event_id,
        source="web",
        surface=surface,
        idempotency_key=idempotency_key,
        metadata={
            "client_id": str(data.get("client_id") or ""),
            "client_action_id": client_action_id,
            "conversation_session_id": _surface_session_id(deps, surface),
            "append_mode": str(data.get("append_mode") or ""),
            "command_confirmed_while_running": bool(
                data.get("command_confirmed_while_running")
            ),
        },
    )


def _record_web_confirmation_input(
    deps: WebDeps,
    surface: str,
    *,
    request: Any,
    decision: str,
    command_text: str,
    client_action_id: str = "",
) -> None:
    detail = request.detail if isinstance(request.detail, dict) else {}
    metadata = request.metadata if isinstance(request.metadata, dict) else {}
    deps.conversation_controller.submit_input(
        ConversationInput(
            ref=_surface_ref(deps, surface),
            text=str(decision or ""),
            raw_event_id=f"input-request:{request.request_id}:{decision}",
            source="web",
            surface=surface,
            metadata={
                "input_kind": ConversationInputKind.CONFIRMATION_RESPONSE.value,
                "conversation_session_id": request.session_id,
                "input_request_id": request.request_id,
                "input_request_kind": ConversationInputRequestKind.CONFIRMATION.value,
                "decision": str(decision or ""),
                "command_text": str(command_text or ""),
                "command_name": str(
                    detail.get("command_name")
                    or metadata.get("command_name")
                    or ""
                ),
                "command_token": str(
                    detail.get("command_token")
                    or metadata.get("command_token")
                    or ""
                ),
                "command_args": str(
                    detail.get("command_args")
                    or metadata.get("command_args")
                    or ""
                ),
                "client_action_id": str(client_action_id or ""),
            },
        )
    )


def _record_web_approval_input(
    deps: WebDeps,
    surface: str,
    data: dict[str, Any],
    *,
    runtime_missing: bool = False,
    runtime_resolved: bool = False,
    runtime_cancelled: bool = False,
    recovery_continuation: bool = False,
) -> None:
    request_id = str(data.get("request_id") or "").strip()
    decision = str(data.get("decision") or "deny").strip() or "deny"
    feedback = str(data.get("feedback") or "")
    scope = str(data.get("scope") or "")
    source = str(data.get("source") or "web").strip() or "web"
    modified_args = (
        data.get("modified_args") if isinstance(data.get("modified_args"), dict) else None
    )
    approval_snapshot = _approval_snapshot_for_request(deps, surface, request_id)
    tool_record_id = str(
        data.get("tool_record_id")
        or (approval_snapshot or {}).get("tool_record_id")
        or ""
    ).strip()
    tool_call_id = str(
        data.get("tool_call_id")
        or (approval_snapshot or {}).get("tool_call_id")
        or ""
    ).strip()
    turn_id = str(
        data.get("turn_id") or (approval_snapshot or {}).get("turn_id") or ""
    ).strip()
    metadata: dict[str, Any] = {
        "input_kind": ConversationInputKind.APPROVAL_RESPONSE.value,
        "conversation_session_id": _surface_session_id(deps, surface),
        "approval_request_id": request_id,
        "decision": decision,
        "feedback": feedback,
        "scope": scope,
        "has_modified_args": modified_args is not None,
        "client_action_id": str(data.get("client_action_id") or ""),
    }
    if tool_record_id:
        metadata["tool_record_id"] = tool_record_id
    if tool_call_id:
        metadata["tool_call_id"] = tool_call_id
    if turn_id:
        metadata["turn_id"] = turn_id
    if runtime_missing:
        metadata["approval_runtime_missing"] = True
    if recovery_continuation:
        metadata["approval_recovery_continuation"] = True
    if runtime_resolved:
        metadata["approval_runtime_resolved"] = True
    if runtime_cancelled:
        metadata["approval_runtime_cancelled"] = True
    if modified_args is not None:
        metadata["modified_args"] = modified_args
    deps.conversation_controller.submit_input(
        ConversationInput(
            ref=_surface_ref(deps, surface),
            text=decision,
            raw_event_id=f"approval:{request_id}:{decision}",
            source=source,
            surface=surface,
            metadata=metadata,
        )
    )


def _approval_snapshot_for_request(
    deps: WebDeps,
    surface: str,
    request_id: str,
) -> dict[str, Any] | None:
    clean_request_id = str(request_id or "").strip()
    if not clean_request_id:
        return None
    repository = deps.conversation_controller.repository
    approval = repository.get_approval(clean_request_id)
    if approval is None:
        return None
    session_id = _surface_session_id(deps, surface)
    if approval.session_id != session_id:
        return None
    if _surface_turn_hidden(repository, session_id, approval.turn_id):
        return None
    return _approval_snapshot_for_ui(deps, approval)


def _approval_snapshot_for_ui(deps: WebDeps, approval: Any) -> dict[str, Any]:
    snapshot = _approval_snapshot(approval)
    mode = (
        getattr(getattr(getattr(deps.settings, "tools", None), "policy", None), "mode", "custom")
        if deps.settings is not None
        else "custom"
    )
    if mode == "always_ask":
        snapshot["actions"] = [
            action for action in snapshot.get("actions", []) if action != "approve_scoped"
        ]
    return snapshot


def _save_permanent_tool_approval(deps: WebDeps, tool_name: str) -> None:
    settings = _current_settings(deps)
    path = settings.paths.agent_config_path
    old_content = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        with file_lock(path):
            set_tool_approval(settings, tool_name, False)
            refreshed = load_settings(
                settings.paths.main_config_path,
                agent_id=settings.paths.agent_id,
            )
    except Exception as error:
        _write_text_atomic(path, old_content)
        raise HTTPException(status_code=400, detail=f"保存永久审批失败：{error}") from error
    deps.coordinator.tool_approvals = dict(refreshed.tools.approvals)


def _is_approval_decision(value: str) -> bool:
    return str(value or "").strip().lower() in {
        "approve",
        "approved",
        "approved_for_session",
        "approve_for_session",
        "yes",
        "1",
    }


def _record_web_agent_control_input(
    deps: WebDeps,
    surface: str,
    data: dict[str, Any],
    *,
    control_action: str,
    cancel_target: CancelTarget,
    target_id: str = "",
    requested_turn_id: str = "",
    agent_id: str = "",
    tool_record_id: str = "",
    tool_call_id: str = "",
    client_action_id: str = "",
) -> None:
    target = str(
        target_id
        or data.get("turn_id")
        or data.get("agent_id")
        or data.get("tool_record_id")
        or data.get("tool_call_id")
        or ""
    ).strip()
    clean_requested_turn_id = str(
        requested_turn_id or data.get("turn_id") or ""
    ).strip()
    client_action_id = str(data.get("client_action_id") or "").strip()
    metadata: dict[str, Any] = {
        "input_kind": ConversationInputKind.AGENT_CONTROL.value,
        "conversation_session_id": _surface_session_id(deps, surface),
        "control_action": str(control_action or ""),
        "cancel_target": cancel_target.value,
        "target_id": target,
        "client_action_id": client_action_id,
    }
    clean_agent_id = str(agent_id or data.get("agent_id") or "").strip()
    if clean_agent_id:
        metadata["agent_id"] = clean_agent_id
    if clean_requested_turn_id:
        metadata["requested_turn_id"] = clean_requested_turn_id
    if tool_record_id:
        metadata["tool_record_id"] = str(tool_record_id)
    if tool_call_id:
        metadata["tool_call_id"] = str(tool_call_id)
    deps.conversation_controller.submit_input(
        ConversationInput(
            ref=_surface_ref(deps, surface),
            text=str(control_action or ""),
            raw_event_id=f"agent-control:{control_action}:{target}",
            idempotency_key=f"agent-control:{surface}:{control_action}:{target}",
            source="web",
            surface=surface,
            metadata=metadata,
        )
    )
    _record_web_stop_requested(
        deps,
        session_id=metadata["conversation_session_id"],
        cancel_target=cancel_target.value,
        requested_turn_id=(
            clean_requested_turn_id or target
            if cancel_target
            in {
                CancelTarget.CURRENT_FOREGROUND_TREE,
                CancelTarget.ENTIRE_AGENT_TREE,
                CancelTarget.AGENT,
            }
            else ""
        ),
        target_id=target,
        source="web",
        control_action=str(control_action or ""),
        agent_id=clean_agent_id,
        tool_record_id=str(tool_record_id or ""),
        tool_call_id=str(tool_call_id or ""),
        client_action_id=client_action_id,
    )


def _record_web_stop_requested(
    deps: WebDeps,
    *,
    session_id: str,
    cancel_target: str = "",
    requested_turn_id: str = "",
    target_id: str = "",
    source: str = "",
    control_action: str = "",
    agent_id: str = "",
    tool_record_id: str = "",
    tool_call_id: str = "",
    client_action_id: str = "",
) -> None:
    record = getattr(deps.conversation_controller, "record_stop_requested", None)
    if callable(record):
        record(
            session_id=session_id,
            cancel_target=cancel_target,
            requested_turn_id=requested_turn_id,
            target_id=target_id,
            source=source,
            control_action=control_action,
            agent_id=agent_id,
            tool_record_id=tool_record_id,
            tool_call_id=tool_call_id,
            client_action_id=client_action_id,
        )
        return
    payload = {
        "cancel_target": str(cancel_target or ""),
        "requested_turn_id": str(requested_turn_id or ""),
        "target_id": str(target_id or requested_turn_id or ""),
        "source": str(source or ""),
        "control_action": str(control_action or ""),
        "client_action_id": str(client_action_id or ""),
    }
    if agent_id:
        payload["agent_id"] = str(agent_id)
    if tool_record_id:
        payload["tool_record_id"] = str(tool_record_id)
    if tool_call_id:
        payload["tool_call_id"] = str(tool_call_id)
    deps.conversation_controller.repository.append_event(
        session_id=session_id,
        event_type="conversation.stop.requested",
        turn_id=str(requested_turn_id or ""),
        payload=payload,
    )


def _record_web_stop_result(
    deps: WebDeps,
    *,
    session_id: str,
    cancelled: bool,
    cancel_target: str = "",
    requested_turn_id: str = "",
    target_id: str = "",
    source: str = "",
    control_action: str = "",
    tool_record_id: str = "",
    tool_call_id: str = "",
    agent_id: str = "",
    cancel_tree: dict[str, Any] | None = None,
    client_action_id: str = "",
) -> None:
    append_stop_result_event(
        deps.conversation_controller.repository,
        session_id=session_id,
        cancelled=cancelled,
        cancel_target=cancel_target,
        requested_turn_id=requested_turn_id,
        target_id=target_id,
        source=source,
        control_action=control_action,
        tool_record_id=tool_record_id,
        tool_call_id=tool_call_id,
        agent_id=agent_id,
        cancel_tree=cancel_tree,
        client_action_id=client_action_id,
    )


def _record_web_event_ack(
    deps: WebDeps,
    surface: str,
    data: dict[str, Any],
) -> None:
    session_id = _surface_session_id(deps, surface)
    reported_session_id = str(data.get("session_id") or "").strip()
    if reported_session_id and reported_session_id != session_id:
        return
    repository = deps.conversation_controller.repository
    reported_seq = safe_int(data.get("latest_event_seq") or data.get("seq") or 0)
    latest_seq = repository.latest_event_seq(session_id)
    repository.record_client_event_ack(
        session_id=session_id,
        client_id=str(data.get("client_id") or "").strip() or "web",
        surface=surface,
        latest_event_seq=min(max(0, reported_seq), latest_seq),
        metadata={
            "source": "websocket",
            "connection_surface": surface,
            "reported_event_seq": reported_seq,
            "latest_event_seq": latest_seq,
        },
    )


def _turn_queued_event(
    result: ConversationSubmitResult | None,
    *,
    action: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if result is None:
        return websocket_server_event("turn.queued")
    payload["input_kind"] = result.classification.kind.value
    payload["session_id"] = result.session_id
    metadata: dict[str, Any] = {}
    if result.message is not None:
        payload["message_id"] = result.message.message_id
        payload["queue_position"] = result.message.queue_position
        metadata.update(result.message.metadata or {})
    if result.turn is not None:
        metadata.update(result.turn.metadata or {})
    client_action_id = str(metadata.get("client_action_id") or "").strip()
    clean_action = str(action or "").strip()
    raw_event_id = str(getattr(result.turn, "raw_event_id", "") or "").strip()
    target_id = str(
        payload.get("message_id")
        or metadata.get("conversation_message_id")
        or raw_event_id
        or ""
    ).strip()
    if clean_action:
        payload["action"] = clean_action
    if client_action_id:
        payload["client_action_id"] = client_action_id
    if raw_event_id:
        payload["raw_event_id"] = raw_event_id
    if clean_action or client_action_id or target_id:
        payload["action_result"] = _action_result_payload(
            ok=True,
            action=clean_action,
            client_action_id=client_action_id,
            target_id=target_id,
            reason="queued",
        )
    return websocket_server_event("turn.queued", **payload)
