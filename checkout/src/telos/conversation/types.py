"""Core conversation input types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from telos.adapters.base import AttachmentRef, ConversationRef


BACKGROUND_NOTIFICATION_EVENT_TYPE = "conversation.background_notification.created"


@dataclass(frozen=True)
class TemporaryConversationRecord:
    session_id: str
    parent_session_id: str
    title: str
    create_action_id: str
    created_at: str
    updated_at: str


class ConversationInputKind(StrEnum):
    NORMAL_MESSAGE = "normal_message"
    REGISTERED_COMMAND = "registered_command"
    UNKNOWN_SLASH_MESSAGE = "unknown_slash_message"
    SKILL_ACTIVATION = "skill_activation"
    APPROVAL_RESPONSE = "approval_response"
    QUESTION_RESPONSE = "question_response"
    CONFIRMATION_RESPONSE = "confirmation_response"
    SYSTEM_TRIGGER = "system_trigger"
    AGENT_CONTROL = "agent_control"
    BACKGROUND_NOTIFICATION = "background_notification"


class CommandAvailability(StrEnum):
    ALWAYS = "always"
    IDLE_ONLY = "idle_only"
    CONFIRM_WHILE_RUNNING = "confirm_while_running"
    QUEUE_WHILE_RUNNING = "queue_while_running"


class CommandContextPolicy(StrEnum):
    NONE = "none"
    USER_MESSAGE = "user_message"
    SYSTEM_REMINDER = "system_reminder"
    EVENT_ONLY = "event_only"


class ConversationAction(StrEnum):
    ENQUEUE_MESSAGE = "enqueue_message"
    RUN_COMMAND = "run_command"
    CANCEL_CURRENT = "cancel_current"
    RECORD_ONLY = "record_only"


class ConversationMessageStatus(StrEnum):
    ACCEPTED = "accepted"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    REPLACED = "replaced"


class ConversationToolCallStatus(StrEnum):
    PENDING = "pending"
    STARTED = "started"
    WAITING_APPROVAL = "waiting_approval"
    APPROVED = "approved"
    DENIED = "denied"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ConversationApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ConversationInputRequestKind(StrEnum):
    APPROVAL = "approval"
    QUESTION = "question"
    CONFIRMATION = "confirmation"


class ConversationInputRequestStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ConversationAttemptStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class ConversationRecoverySummary:
    messages_requeued: int = 0
    messages_resume_required: int = 0
    messages_failed: int = 0
    message_leases_released: int = 0
    tool_calls_recovered: int = 0
    tool_calls_cancelled: int = 0
    tool_calls_failed: int = 0
    approvals_recovered: int = 0
    approvals_cancelled: int = 0
    input_requests_recovered: int = 0
    input_requests_cancelled: int = 0
    attempts_failed: int = 0
    runs_interrupted: int = 0
    queued_runs_interrupted: int = 0
    running_runs_interrupted: int = 0
    cancel_requested_runs_interrupted: int = 0
    foreground_runs_interrupted: int = 0
    background_runs_interrupted: int = 0

    @property
    def changed(self) -> bool:
        return any(
            (
                self.messages_requeued,
                self.messages_resume_required,
                self.messages_failed,
                self.message_leases_released,
                self.tool_calls_recovered,
                self.tool_calls_cancelled,
                self.tool_calls_failed,
                self.approvals_recovered,
                self.approvals_cancelled,
                self.input_requests_recovered,
                self.input_requests_cancelled,
                self.attempts_failed,
                self.runs_interrupted,
                self.queued_runs_interrupted,
                self.running_runs_interrupted,
                self.cancel_requested_runs_interrupted,
                self.foreground_runs_interrupted,
                self.background_runs_interrupted,
            )
        )


class ConversationAppendMode(StrEnum):
    NEXT_TURN = "next_turn"
    APPEND_CURRENT_TURN = "append_current_turn"
    DRAFT = "draft"


class CancelTarget(StrEnum):
    MESSAGE = "message"
    AGENT = "agent"
    TOOL_CALL = "tool_call"
    APPROVAL = "approval"
    INPUT_REQUEST = "input_request"
    BACKGROUND_TASK = "background_task"
    CURRENT_FOREGROUND_TREE = "current_foreground_tree"
    ENTIRE_AGENT_TREE = "entire_agent_tree"


@dataclass(frozen=True)
class ConversationInput:
    ref: ConversationRef
    text: str = ""
    attachments: list[AttachmentRef] = field(default_factory=list)
    raw_event_id: str = ""
    is_group: bool = False
    mentions_bot: bool = False
    timestamp: datetime | None = None
    reply_to: str = ""
    source: str = ""
    surface: str = ""
    idempotency_key: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InputClassification:
    kind: ConversationInputKind
    normalized_text: str
    command_name: str = ""
    command_token: str = ""
    command_args: str = ""
    command_availability: CommandAvailability | None = None
    command_context_policy: CommandContextPolicy | None = None
    creates_turn: bool = False
    cancel_target: CancelTarget | None = None
    skill_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def enters_model(self) -> bool:
        if self.kind in {
            ConversationInputKind.NORMAL_MESSAGE,
            ConversationInputKind.UNKNOWN_SLASH_MESSAGE,
            ConversationInputKind.SKILL_ACTIVATION,
            ConversationInputKind.SYSTEM_TRIGGER,
        }:
            return True
        return self.command_context_policy in {
            CommandContextPolicy.USER_MESSAGE,
            CommandContextPolicy.SYSTEM_REMINDER,
        }


@dataclass(frozen=True)
class ConversationMessageRecord:
    message_id: str
    session_id: str
    ref: ConversationRef
    kind: ConversationInputKind
    status: ConversationMessageStatus
    text: str
    attachments: list[AttachmentRef] = field(default_factory=list)
    source: str = ""
    platform: str = ""
    raw_event_id: str = ""
    idempotency_key: str = ""
    queue_position: int = 0
    turn_id: str = ""
    retry_of_message_id: str = ""
    append_mode: ConversationAppendMode = ConversationAppendMode.NEXT_TURN
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ConversationMessageLeaseRecord:
    message_id: str
    session_id: str
    worker_id: str
    lease_token: str
    leased_at: str
    heartbeat_at: str
    expires_at: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ConversationTurnAttemptRecord:
    attempt_id: str
    session_id: str
    message_id: str
    attempt_index: int
    status: ConversationAttemptStatus
    retry_of_attempt_id: str = ""
    retry_source: str = ""
    turn_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ConversationEventRecord:
    event_id: str
    session_id: str
    seq: int
    type: str
    message_id: str = ""
    turn_id: str = ""
    agent_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""


@dataclass(frozen=True)
class ConversationClientAckRecord:
    session_id: str
    client_id: str
    surface: str
    latest_event_seq: int
    metadata: dict[str, Any] = field(default_factory=dict)
    updated_at: str = ""


@dataclass(frozen=True)
class ConversationInputRequestRecord:
    request_id: str
    session_id: str
    kind: ConversationInputRequestKind
    status: ConversationInputRequestStatus
    title: str = ""
    prompt: str = ""
    source_type: str = ""
    source_id: str = ""
    turn_id: str = ""
    agent_id: str = ""
    tool_record_id: str = ""
    tool_call_id: str = ""
    detail: Any = None
    response: Any = None
    decision: str = ""
    scope: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    requested_at: str = ""
    resolved_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ConversationToolCallRecord:
    tool_record_id: str
    session_id: str
    turn_id: str
    tool_call_id: str
    tool_name: str
    status: ConversationToolCallStatus
    step: int = 0
    args: dict[str, Any] = field(default_factory=dict)
    approval_request_id: str = ""
    child_turn_id: str = ""
    result: Any = None
    is_error: bool = False
    error: str = ""
    retryable: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    completed_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ConversationApprovalRecord:
    approval_id: str
    session_id: str
    turn_id: str
    tool_record_id: str
    tool_call_id: str
    tool_name: str
    status: ConversationApprovalStatus
    action: str = ""
    detail: Any = None
    decision: str = ""
    feedback: str = ""
    scope: str = ""
    modified_args: dict[str, Any] = field(default_factory=dict)
    requested_at: str = ""
    resolved_at: str = ""
    updated_at: str = ""
