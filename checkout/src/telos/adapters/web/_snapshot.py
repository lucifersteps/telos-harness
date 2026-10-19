"""Web 快照投影：record → dict 的纯投影/标签/动作列表帮助函数。

从 server.py 抽出的模块级快照投影函数，无 create_app 闭包依赖，
只依赖 telos.* 领域模型与 stdlib，避免与 server.py 形成循环导入。
"""

from __future__ import annotations

import json
from typing import Any

from telos.conversation.labels import (
    command_availability_label,
    command_context_policy_label,
)
from telos.conversation.types import (
    CommandContextPolicy,
    ConversationApprovalStatus,
    ConversationInputRequestKind,
    ConversationInputRequestStatus,
    ConversationMessageStatus,
    ConversationToolCallStatus,
)
from telos.core.coerce import safe_int

_TOOL_RESULT_PREVIEW_CHARS = 4000

_QUEUE_MESSAGE_CHANGE_TYPES = {
    "conversation.message.queued",
    "conversation.message.edited",
    "conversation.message.append_mode_changed",
    "conversation.message.promoted",
    "conversation.message.merged",
    "conversation.message.replaced",
    "conversation.message.resume_required",
    "conversation.message.resume_requested",
    "conversation.message.running",
    "conversation.message.waiting_approval",
    "conversation.message.completed",
    "conversation.message.cancelled",
    "conversation.message.failed",
    "conversation.message.appended_to_current_turn",
    "conversation.message.append_consumed",
    "conversation.message.append_unconsumed",
}

def _input_command_definition_snapshot(definition: Any) -> dict[str, Any]:
    context_policy = getattr(definition, "context_policy", None)
    availability = getattr(definition, "availability", None)
    cancel_target = getattr(definition, "cancel_target", None)
    return {
        "name": str(getattr(definition, "name", "") or ""),
        "aliases": [
            str(alias or "").strip()
            for alias in list(getattr(definition, "aliases", ()) or ())
            if str(alias or "").strip()
        ],
        "exact_text": [
            str(text or "").strip()
            for text in list(getattr(definition, "exact_text", ()) or ())
            if str(text or "").strip()
        ],
        "help_commands": [
            str(command or "").strip()
            for command in list(getattr(definition, "help_commands", ()) or ())
            if str(command or "").strip()
        ],
        "help_visible": bool(getattr(definition, "help_visible", True)),
        "availability": str(getattr(availability, "value", availability) or ""),
        "context_policy": str(getattr(context_policy, "value", context_policy) or ""),
        "creates_turn": bool(getattr(definition, "creates_turn", False)),
        "enters_model": context_policy
        in {CommandContextPolicy.USER_MESSAGE, CommandContextPolicy.SYSTEM_REMINDER},
        "cancel_target": str(getattr(cancel_target, "value", cancel_target) or ""),
        "description": str(getattr(definition, "description", "") or ""),
    }

def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result

def _client_event_ack_snapshot(
    repository: Any,
    *,
    session_id: str,
    surface: str,
    client_id: str,
    latest_event_seq: int,
) -> dict[str, Any]:
    clean_client_id = str(client_id or "").strip()
    clean_surface = str(surface or "").strip()
    server_latest_seq = max(0, int(latest_event_seq or 0))
    snapshot = {
        "client_id": clean_client_id,
        "surface": clean_surface,
        "latest_event_seq": 0,
        "server_latest_event_seq": server_latest_seq,
        "has_ack": False,
        "updated_at": "",
        "metadata": {},
    }
    if not clean_client_id:
        return snapshot
    ack = repository.get_client_event_ack(
        session_id=str(session_id or "").strip(),
        client_id=clean_client_id,
        surface=clean_surface,
    )
    if ack is None:
        return snapshot
    return {
        **snapshot,
        "latest_event_seq": max(
            0,
            min(int(ack.latest_event_seq or 0), server_latest_seq),
        ),
        "has_ack": True,
        "updated_at": str(ack.updated_at or ""),
        "metadata": dict(ack.metadata or {}),
    }

def _session_actions(
    *,
    active_turns: list[Any],
    pending_approvals: list[Any],
    pending_input_requests: list[Any],
    scheduler_active: bool = False,
    can_edit_last_message: bool = True,
) -> list[str]:
    actions = [
        "send",
        "append_current_turn",
        "save_draft",
        "ack_event_position",
    ]
    if can_edit_last_message:
        # 没有可编辑消息时前端仍会按是否存在用户消息决定是否露出入口。
        actions.append("edit_last_message")
    if active_turns or scheduler_active:
        actions.extend(["cancel_turn", "cancel_agent_tree"])
    if pending_approvals:
        actions.extend(["approval", "cancel_approval"])
    input_request_kinds = {
        getattr(request, "kind", None) for request in pending_input_requests
    }
    if ConversationInputRequestKind.CONFIRMATION in input_request_kinds:
        actions.append("confirm_command")
    if ConversationInputRequestKind.QUESTION in input_request_kinds:
        actions.append("input_request_response")
    if input_request_kinds & {
        ConversationInputRequestKind.CONFIRMATION,
        ConversationInputRequestKind.QUESTION,
    }:
        actions.append("cancel_input_request")
    return actions

def _queue_snapshot(
    repository: Any,
    session_id: str,
    messages: list[Any],
    *,
    failed_retryable: bool,
) -> dict[str, Any]:
    queued = [
        message
        for message in messages
        if message.status == ConversationMessageStatus.QUEUED
    ]
    order = []
    for message in sorted(
        queued,
        key=lambda item: (
            int(getattr(item, "queue_position", 0) or 0),
            str(getattr(item, "created_at", "") or ""),
            str(getattr(item, "message_id", "") or ""),
        ),
    ):
        metadata = getattr(message, "metadata", {}) or {}
        resume_required = bool(metadata.get("requires_explicit_resume"))
        order.append(
            {
                "message_id": message.message_id,
                "queue_position": message.queue_position,
                "append_mode": message.append_mode.value,
                "requires_explicit_resume": resume_required,
                "source": message.source,
                "platform": message.platform,
                "actions": _queued_message_actions(resume_required),
            }
        )
    running = [
        {
            "message_id": message.message_id,
            "status": message.status.value,
            "turn_id": message.turn_id,
            "source": message.source,
            "platform": message.platform,
            "actions": [],
        }
        for message in messages
        if message.status in {
            ConversationMessageStatus.RUNNING,
            ConversationMessageStatus.WAITING_APPROVAL,
        }
    ]
    retried_message_ids = {
        str(getattr(message, "retry_of_message_id", "") or "")
        for message in messages
        if str(getattr(message, "retry_of_message_id", "") or "")
    }
    failed = [
        {
            "message_id": message.message_id,
            "failure_reason": _message_error_text(
                message,
                dict(getattr(message, "metadata", {}) or {}),
            ),
            "retry_source": str(
                (getattr(message, "metadata", {}) or {}).get("retry_source") or ""
            ),
            "actions": _failed_message_actions(retryable=failed_retryable),
        }
        for message in messages
        if message.status == ConversationMessageStatus.FAILED
        and message.message_id not in retried_message_ids
    ]
    last_reordered = _latest_queue_reordered_snapshot(repository, session_id)
    last_changed = _latest_queue_changed_snapshot(repository, session_id)
    resume_required_count = sum(
        1 for item in order if bool(item.get("requires_explicit_resume"))
    )
    runnable_count = max(0, len(order) - resume_required_count)
    return {
        "revision": repository.queue_revision(session_id),
        "actions": ["reorder", "cancel"],
        "order": order,
        "running": running,
        "failed": failed,
        "queued_count": len(order),
        "runnable_count": runnable_count,
        "resume_required_count": resume_required_count,
        "running_count": len(running),
        "failed_count": len(failed),
        "last_changed": last_changed,
        "last_reordered": last_reordered,
    }

def _queued_message_actions(resume_required: bool) -> list[str]:
    actions = ["promote", "edit"]
    if resume_required:
        actions.insert(0, "resume")
    else:
        actions.append("append_current_turn")
    actions.append("cancel")
    return actions

def _failed_message_actions(*, retryable: bool) -> list[str]:
    if retryable:
        return ["retry"]
    return []

def _latest_queue_changed_snapshot(
    repository: Any,
    session_id: str,
) -> dict[str, Any] | None:
    for event in reversed(repository.list_events_page(session_id, limit=500)):
        if (
            event.type != "conversation.queue.reordered"
            and event.type not in _QUEUE_MESSAGE_CHANGE_TYPES
        ):
            continue
        payload = dict(event.payload or {})
        return {
            "seq": event.seq,
            "event_id": event.event_id,
            "type": event.type,
            "message_id": event.message_id,
            "turn_id": event.turn_id,
            "reason": str(payload.get("reason") or ""),
            "created_at": event.created_at,
        }
    return None

def _latest_queue_reordered_snapshot(
    repository: Any,
    session_id: str,
) -> dict[str, Any] | None:
    for event in reversed(repository.list_events_page(session_id, limit=500)):
        if event.type != "conversation.queue.reordered":
            continue
        payload = dict(event.payload or {})
        return {
            "seq": event.seq,
            "event_id": event.event_id,
            "message_id": event.message_id,
            "reason": str(payload.get("reason") or ""),
            "order": list(payload.get("order") or []),
            "created_at": event.created_at,
        }
    return None

def _latest_draft_snapshot(repository: Any, session_id: str) -> dict[str, Any] | None:
    for event in reversed(repository.list_events_page(session_id, limit=500)):
        if event.type == "conversation.message.accepted":
            return None
        if event.type != "conversation.draft.saved":
            continue
        payload = dict(event.payload or {})
        text = str(payload.get("text") or "")
        if not text:
            return None
        return {
            "event_id": event.event_id,
            "seq": event.seq,
            "text": text,
            "source": str(payload.get("source") or ""),
            "surface": str(payload.get("surface") or ""),
            "append_mode": str(payload.get("append_mode") or "draft"),
            "metadata": dict(payload.get("metadata") or {}),
            "created_at": event.created_at,
        }
    return None

def _latest_recovery_snapshot(repository: Any, session_id: str) -> dict[str, Any] | None:
    events = repository.list_events_page(session_id, limit=300)
    for event in reversed(events):
        if event.type != "conversation.recovery.completed":
            continue
        payload = dict(event.payload or {})
        previous_recovery_seq = max(
            (
                item.seq
                for item in events
                if item.type == "conversation.recovery.completed"
                and item.seq < event.seq
            ),
            default=0,
        )
        return {
            "seq": event.seq,
            "created_at": event.created_at,
            "reason": str(payload.get("reason") or ""),
            "summary": {
                key: safe_int(payload.get(key) or 0)
                for key in (
                    "messages_requeued",
                    "messages_resume_required",
                    "messages_failed",
                    "message_leases_released",
                    "tool_calls_recovered",
                    "tool_calls_cancelled",
                    "tool_calls_failed",
                    "approvals_recovered",
                    "approvals_cancelled",
                    "input_requests_recovered",
                    "input_requests_cancelled",
                    "attempts_failed",
                    "runs_interrupted",
                    "queued_runs_interrupted",
                    "running_runs_interrupted",
                    "cancel_requested_runs_interrupted",
                    "foreground_runs_interrupted",
                    "background_runs_interrupted",
                )
                if safe_int(payload.get(key) or 0)
            },
            "runs": [
                _recovered_run_snapshot(item)
                for item in events
                if item.type == "conversation.run.recovered"
                and previous_recovery_seq < item.seq < event.seq
            ][:20],
        }
    return None

def _recovered_run_snapshot(event: Any) -> dict[str, Any]:
    payload = dict(getattr(event, "payload", {}) or {})
    background = bool(payload.get("background")) or str(
        payload.get("child_scope") or ""
    ).strip().lower() == "background"
    return {
        "seq": int(getattr(event, "seq", 0) or 0),
        "created_at": str(getattr(event, "created_at", "") or ""),
        "turn_id": str(payload.get("turn_id") or getattr(event, "turn_id", "") or ""),
        "agent_id": str(payload.get("agent_id") or getattr(event, "agent_id", "") or ""),
        "old_status": str(payload.get("old_status") or ""),
        "status": str(payload.get("status") or "failed"),
        "reason": str(payload.get("reason") or ""),
        "child_scope": "background" if background else "foreground",
        "background": background,
        "parent_turn_id": str(payload.get("parent_turn_id") or ""),
        "parent_tool_call_id": str(payload.get("parent_tool_call_id") or ""),
        "started_at": str(payload.get("started_at") or ""),
        "finished_at": str(payload.get("finished_at") or ""),
    }

def _tool_call_recovery_snapshot(tool_call: Any) -> dict[str, Any]:
    metadata = dict(getattr(tool_call, "metadata", {}) or {})
    return {
        "runtime_missing": bool(metadata.get("recovery_runtime_missing")),
        "reason": str(metadata.get("recovery_reason") or ""),
        "recovered_at": str(metadata.get("recovered_at") or ""),
    }

def _message_error_text(message: Any, metadata: dict[str, Any]) -> str:
    for key in ("error", "failure_reason", "reason", "recovery_resume_reason"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    delivery = metadata.get("delivery")
    if isinstance(delivery, dict):
        value = str(delivery.get("error") or "").strip()
        if value:
            return value
    text = str(getattr(message, "text", "") or "").strip()
    return text[:120] if text else "消息没有完成。"

def _conversation_events_page(
    repository: Any,
    session_id: str,
    *,
    after_seq: int,
    limit: int,
) -> dict[str, Any]:
    clean_after_seq = max(0, int(after_seq))
    clean_limit = max(1, min(1000, int(limit)))
    events = repository.list_events(
        session_id,
        after_seq=clean_after_seq,
        limit=clean_limit,
    )
    latest_seq = repository.latest_event_seq(session_id)
    next_after_seq = events[-1].seq if events else clean_after_seq
    return {
        "session_id": session_id,
        "latest_event_seq": latest_seq,
        "next_after_seq": next_after_seq,
        "has_more": next_after_seq < latest_seq,
        "events": events,
    }

def _runtime_cancel_tree_snapshot(
    runtime_view: Any,
    turn_id: str,
    *,
    include_background: bool,
) -> dict[str, Any]:
    clean_turn_id = str(turn_id or "").strip()
    describe = getattr(runtime_view, "describe_cancel_tree", None)
    if not clean_turn_id or not callable(describe):
        return {}
    try:
        snapshot = describe(clean_turn_id, include_background=include_background)
    except Exception:  # noqa: BLE001 - 停止结果不能因为审计增强失败而失败
        return {}
    return dict(snapshot or {})

def _conversation_event_snapshot(event: Any) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "session_id": event.session_id,
        "seq": event.seq,
        "type": event.type,
        "message_id": event.message_id,
        "turn_id": event.turn_id,
        "agent_id": event.agent_id,
        "payload": dict(event.payload or {}),
        "created_at": event.created_at,
    }

def _message_snapshot(message: Any) -> dict[str, Any]:
    metadata = dict(getattr(message, "metadata", {}) or {})
    status_value = message.status.value
    failure_reason = (
        _message_error_text(message, metadata)
        if message.status == ConversationMessageStatus.FAILED
        else ""
    )
    return {
        "message_id": message.message_id,
        "kind": message.kind.value,
        "status": status_value,
        "text": message.text,
        "source": message.source,
        "platform": message.platform,
        "queue_position": message.queue_position,
        "turn_id": message.turn_id,
        "retry_of_message_id": message.retry_of_message_id,
        "retry_of_attempt_id": str(metadata.get("retry_of_attempt_id") or ""),
        "retry_source": str(metadata.get("retry_source") or ""),
        "retry_original_append_mode": str(
            metadata.get("retry_original_append_mode") or ""
        ),
        "retry_of_tool_record_id": str(metadata.get("retry_of_tool_record_id") or ""),
        "retry_of_tool_call_id": str(metadata.get("retry_of_tool_call_id") or ""),
        "retry_tool_name": str(metadata.get("retry_tool_name") or ""),
        "retry_after_turn_id": str(metadata.get("retry_after_turn_id") or ""),
        "redo_exclude_turn_ids": [
            str(turn_id or "").strip()
            for turn_id in list(metadata.get("redo_exclude_turn_ids") or [])
            if str(turn_id or "").strip()
        ],
        "retry_after_tool_record_id": str(
            metadata.get("retry_after_tool_record_id") or ""
        ),
        "retry_after_tool_call_id": str(
            metadata.get("retry_after_tool_call_id") or ""
        ),
        "retry_after_tool_name": str(metadata.get("retry_after_tool_name") or ""),
        "append_mode": message.append_mode.value,
        "append_target_message_id": str(
            metadata.get("append_target_message_id") or ""
        ),
        "append_target_attempt_id": str(
            metadata.get("append_target_attempt_id") or ""
        ),
        "append_finalize_reason": str(metadata.get("append_finalize_reason") or ""),
        "failure_reason": failure_reason,
        "requires_explicit_resume": bool(
            metadata.get("requires_explicit_resume")
        ),
        "recovery_resume_reason": str(
            metadata.get("recovery_resume_reason") or ""
        ),
        "created_at": message.created_at,
        "updated_at": message.updated_at,
    }

def _turn_run_snapshot(run: Any) -> dict[str, Any]:
    metadata = dict(getattr(run, "metadata", {}) or {})
    return {
        "turn_id": run.turn_id,
        "message_id": str(metadata.get("conversation_message_id") or ""),
        "status": run.status,
        "started_at": run.started_at,
        "parent_turn_id": run.parent_turn_id,
        "parent_tool_call_id": run.parent_tool_call_id,
        "source_platform": run.source_platform,
        "source_id": run.source_id,
        "chat_id": run.chat_id,
        "thread_id": run.thread_id,
        "user_id": run.user_id,
        "raw_event_id": run.raw_event_id,
        "cancel_requested_at": run.cancel_requested_at,
        "stop_reason": run.stop_reason,
        "error": run.error,
    }

def _tool_call_snapshot(tool_call: Any, *, agent_id: str = "") -> dict[str, Any]:
    result_snapshot = _tool_result_snapshot(getattr(tool_call, "result", None))
    recovery = _tool_call_recovery_snapshot(tool_call)
    return {
        "tool_record_id": tool_call.tool_record_id,
        "turn_id": tool_call.turn_id,
        "agent_id": str(agent_id or ""),
        "tool_call_id": tool_call.tool_call_id,
        "tool_name": tool_call.tool_name,
        "status": tool_call.status.value,
        "step": tool_call.step,
        "args": dict(tool_call.args or {}),
        "metadata": dict(tool_call.metadata or {}),
        "approval_request_id": tool_call.approval_request_id,
        "child_turn_id": tool_call.child_turn_id,
        "result": result_snapshot["result"],
        "result_preview": result_snapshot["preview"],
        "result_truncated": result_snapshot["truncated"],
        "is_error": tool_call.is_error,
        "error": tool_call.error,
        "retryable": tool_call.retryable,
        "recovery": recovery,
        "recovery_runtime_missing": bool(recovery.get("runtime_missing")),
        "recovery_reason": str(recovery.get("reason") or ""),
        "recovered_at": str(recovery.get("recovered_at") or ""),
        "actions": _tool_call_actions(tool_call),
        "started_at": tool_call.started_at,
        "completed_at": tool_call.completed_at,
        "updated_at": tool_call.updated_at,
    }

def _tool_call_actions(tool_call: Any) -> list[str]:
    status = tool_call.status
    if status == ConversationToolCallStatus.FAILED and bool(tool_call.retryable):
        return ["retry_tool_call"]
    if status in {
        ConversationToolCallStatus.STARTED,
        ConversationToolCallStatus.APPROVED,
    }:
        return ["cancel_tool_call"]
    return []

def _tool_result_snapshot(value: Any) -> dict[str, Any]:
    if value is None:
        return {"result": None, "preview": "", "truncated": False}
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=str,
        )
    truncated = len(text) > _TOOL_RESULT_PREVIEW_CHARS
    preview = text
    if truncated:
        preview = text[:_TOOL_RESULT_PREVIEW_CHARS] + "\n[结果已截断]"
    return {
        "result": None if truncated else value,
        "preview": preview,
        "truncated": truncated,
    }

def _approval_snapshot(approval: Any) -> dict[str, Any]:
    return {
        "approval_id": approval.approval_id,
        "turn_id": approval.turn_id,
        "tool_record_id": approval.tool_record_id,
        "tool_call_id": approval.tool_call_id,
        "tool_name": approval.tool_name,
        "status": approval.status.value,
        "action": approval.action,
        "detail": approval.detail,
        "decision": approval.decision,
        "feedback": approval.feedback,
        "scope": approval.scope,
        "modified_args": dict(approval.modified_args or {}),
        "actions": _approval_actions(approval),
        "requested_at": approval.requested_at,
        "resolved_at": approval.resolved_at,
        "updated_at": approval.updated_at,
    }

def _approval_actions(approval: Any) -> list[str]:
    if approval.status != ConversationApprovalStatus.PENDING:
        return []
    return _pending_approval_actions()

def _pending_approval_actions() -> list[str]:
    return [
        "approve",
        "approve_scoped",
        "explain",
        "deny",
    ]

def _input_request_snapshot(request: Any) -> dict[str, Any]:
    command = _input_request_command_snapshot(request)
    return {
        "request_id": request.request_id,
        "kind": request.kind.value,
        "status": request.status.value,
        "title": request.title,
        "prompt": request.prompt,
        "summary": _input_request_summary(request, command=command),
        "source_type": request.source_type,
        "source_id": request.source_id,
        "turn_id": request.turn_id,
        "agent_id": request.agent_id,
        "tool_record_id": request.tool_record_id,
        "tool_call_id": request.tool_call_id,
        "detail": request.detail,
        "command": command,
        "response": request.response,
        "decision": request.decision,
        "scope": request.scope,
        "metadata": dict(request.metadata or {}),
        "actions": _input_request_actions(request),
        "requested_at": request.requested_at,
        "resolved_at": request.resolved_at,
        "updated_at": request.updated_at,
    }

def _input_request_command_snapshot(request: Any) -> dict[str, Any] | None:
    if request.kind != ConversationInputRequestKind.CONFIRMATION:
        return None
    detail = request.detail if isinstance(request.detail, dict) else {}
    metadata = dict(getattr(request, "metadata", {}) or {})
    command_text = str(
        detail.get("command_text")
        or detail.get("command_token")
        or metadata.get("command_token")
        or getattr(request, "source_id", "")
        or ""
    ).strip()
    return {
        "text": command_text,
        "name": str(detail.get("command_name") or metadata.get("command_name") or ""),
        "token": str(detail.get("command_token") or metadata.get("command_token") or ""),
        "args": str(detail.get("command_args") or metadata.get("command_args") or ""),
        "reason": str(detail.get("reason") or getattr(request, "prompt", "") or ""),
        "availability": str(detail.get("command_availability") or ""),
        "availability_label": command_availability_label(
            str(detail.get("command_availability") or "")
        ),
        "context_policy": str(detail.get("command_context_policy") or ""),
        "context_policy_label": command_context_policy_label(
            str(detail.get("command_context_policy") or "")
        ),
        "creates_turn": bool(detail.get("creates_turn")),
        "enters_model": bool(detail.get("enters_model")),
        "source": str(detail.get("source") or metadata.get("source") or ""),
        "surface": str(detail.get("surface") or metadata.get("surface") or ""),
    }

def _input_request_summary(
    request: Any,
    *,
    command: dict[str, Any] | None = None,
) -> str:
    if request.kind == ConversationInputRequestKind.CONFIRMATION:
        command_data = command or _input_request_command_snapshot(request) or {}
        command_text = str(command_data.get("text") or "").strip()
        reason = str(command_data.get("reason") or "").strip()
        prefix = f"确认后执行 {command_text}" if command_text else "确认后执行命令"
        return prefix + (f"。原因：{reason}" if reason else "。")
    return str(getattr(request, "prompt", "") or getattr(request, "title", "") or "")

def _input_request_actions(request: Any) -> list[str]:
    if request.status != ConversationInputRequestStatus.PENDING:
        return []
    if request.kind == ConversationInputRequestKind.CONFIRMATION:
        return ["confirm_command", "cancel_input_request"]
    if request.kind == ConversationInputRequestKind.QUESTION:
        detail = dict(request.detail or {}) if isinstance(request.detail, dict) else {}
        actions: list[str] = []
        options = detail.get("options")
        if isinstance(options, list) and options:
            actions.append("answer_option")
        if detail.get("allow_free_text") is not False:
            actions.append("answer_free_text")
        actions.append("cancel_input_request")
        return actions
    if request.kind == ConversationInputRequestKind.APPROVAL:
        return _pending_approval_actions()
    return []

