"""Agent event contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from telos.agent.loop.retry import ProviderRetryEvent
from telos.providers.base import ChatRequest, Message


AgentEventDict = dict[str, Any]


@dataclass(frozen=True)
class AgentEvent:
    type: str
    turn_id: str
    step: int | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> AgentEventDict:
        event: AgentEventDict = {"type": self.type, "turn_id": self.turn_id}
        if self.step is not None:
            event["step"] = self.step
        event.update(self.data)
        return event


@dataclass(frozen=True)
class StepBeginEvent:
    turn_id: str
    step: int
    message_count: int | None = None

    def to_dict(self) -> AgentEventDict:
        data = {}
        if self.message_count is not None:
            data["message_count"] = self.message_count
        return AgentEvent("step.begin", self.turn_id, self.step, data).to_dict()


@dataclass(frozen=True)
class StepEndEvent:
    turn_id: str
    step: int

    def to_dict(self) -> AgentEventDict:
        return AgentEvent("step.end", self.turn_id, self.step).to_dict()


@dataclass(frozen=True)
class ProviderRetryAgentEvent:
    turn_id: str
    step: int
    retry: ProviderRetryEvent

    def to_dict(self) -> AgentEventDict:
        return AgentEvent(
            "provider.retry",
            self.turn_id,
            self.step,
            self.retry.to_dict(),
        ).to_dict()


@dataclass(frozen=True)
class ProviderRequestAgentEvent:
    turn_id: str
    step: int
    request: ChatRequest

    def to_dict(self) -> AgentEventDict:
        return AgentEvent(
            "provider.request",
            self.turn_id,
            self.step,
            {"request": self.request},
        ).to_dict()


#: Maps a raw provider stream ``LLMEvent.type`` to the agent delta kind. Only
#: these three stream as live, displayable deltas; other provider events
#: (``message``, ``done``) surface through ``context.message``/terminal events.
_PROVIDER_DELTA_KINDS = {
    "text_delta": "text",
    "thinking_delta": "thinking",
    "tool_call_delta": "tool_call",
}


def provider_delta_kind(provider_event_type: str) -> str | None:
    return _PROVIDER_DELTA_KINDS.get(provider_event_type)


@dataclass(frozen=True)
class LlmDeltaEvent:
    turn_id: str
    step: int
    delta_type: str  # "text" | "thinking" | "tool_call"
    delta: Any

    def to_dict(self) -> AgentEventDict:
        return AgentEvent(
            f"llm.{self.delta_type}.delta",
            self.turn_id,
            self.step,
            {"delta": self.delta},
        ).to_dict()


@dataclass(frozen=True)
class ProviderUsageAgentEvent:
    turn_id: str
    step: int
    usage: dict[str, Any]

    def to_dict(self) -> AgentEventDict:
        return AgentEvent(
            "provider.usage",
            self.turn_id,
            self.step,
            {"usage": dict(self.usage)},
        ).to_dict()


@dataclass(frozen=True)
class ContextMessageEvent:
    turn_id: str
    step: int
    message: Message

    def to_dict(self) -> AgentEventDict:
        return AgentEvent(
            "context.message",
            self.turn_id,
            self.step,
            {"message": self.message},
        ).to_dict()


@dataclass(frozen=True)
class ToolResultEvent:
    turn_id: str
    step: int
    tool_call_id: str
    tool_name: str
    args: dict[str, Any]
    is_error: bool
    output: object
    error: str = ""
    retryable: bool = False
    policy_audit: dict[str, Any] | None = None

    def to_dict(self) -> AgentEventDict:
        data: dict[str, Any] = {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "args": self.args,
            "is_error": self.is_error,
            "output": self.output,
            "policy_audit": self.policy_audit,
        }
        if self.error:
            data["error"] = self.error
        if self.retryable:
            data["retryable"] = self.retryable
        return AgentEvent(
            "tool.result",
            self.turn_id,
            self.step,
            data,
        ).to_dict()


def step_begin_event(
    *,
    turn_id: str,
    step: int,
    message_count: int | None = None,
) -> AgentEventDict:
    return StepBeginEvent(
        turn_id=turn_id,
        step=step,
        message_count=message_count,
    ).to_dict()


def step_end_event(*, turn_id: str, step: int) -> AgentEventDict:
    return StepEndEvent(turn_id=turn_id, step=step).to_dict()


def provider_retry_event(
    *,
    turn_id: str,
    step: int,
    retry: ProviderRetryEvent,
) -> AgentEventDict:
    return ProviderRetryAgentEvent(turn_id=turn_id, step=step, retry=retry).to_dict()


def provider_request_event(
    *,
    turn_id: str,
    step: int,
    request: ChatRequest,
) -> AgentEventDict:
    return ProviderRequestAgentEvent(turn_id=turn_id, step=step, request=request).to_dict()


def llm_delta_event(
    *,
    turn_id: str,
    step: int,
    delta_type: str,
    delta: Any,
) -> AgentEventDict:
    return LlmDeltaEvent(
        turn_id=turn_id,
        step=step,
        delta_type=delta_type,
        delta=delta,
    ).to_dict()


def provider_usage_event(
    *,
    turn_id: str,
    step: int,
    usage: dict[str, Any],
) -> AgentEventDict:
    return ProviderUsageAgentEvent(turn_id=turn_id, step=step, usage=usage).to_dict()


def context_message_event(*, turn_id: str, step: int, message: Message) -> AgentEventDict:
    return ContextMessageEvent(
        turn_id=turn_id,
        step=step,
        message=message,
    ).to_dict()


TURN_TERMINAL_TYPES = frozenset({"turn.completed", "turn.failed", "turn.cancelled"})


def turn_started_event(
    *,
    turn_id: str,
    session_id: str,
    agent_id: str,
    source: dict[str, Any],
    message_id: str = "",
) -> AgentEventDict:
    return AgentEvent(
        "turn.started",
        turn_id,
        None,
        {
            "session_id": session_id,
            "agent_id": agent_id,
            "source": dict(source),
            "message_id": str(message_id or ""),
        },
    ).to_dict()


def inbound_message_received_event(
    *,
    turn_id: str,
    text_preview: str,
    text_chars: int,
    message_from: str,
    message_id: str = "",
    raw_event_id: str = "",
    origin: str = "",
    input_kind: str = "",
) -> AgentEventDict:
    """本轮输入（可见文本）。进 live stream + run_events，使任意 attach 的视图（web 看
    微信、助手看 main 委派）都能渲染这条输入。``origin`` 标注来源：``"wechat"`` / ``"telos"``
    （main 委派给助手）/ 空（本端 web 用户）。"""
    data = {
        "text_preview": text_preview,
        "text_chars": text_chars,
        "message_from": message_from,
        "message_id": message_id,
        "raw_event_id": raw_event_id,
        "origin": origin,
    }
    if input_kind:
        data["input_kind"] = input_kind
    return AgentEvent(
        "inbound.message.received",
        turn_id,
        None,
        data,
    ).to_dict()


def assistant_message_completed_event(
    *,
    turn_id: str,
    step: int,
    content: str,
    tool_calls: list[dict[str, Any]],
) -> AgentEventDict:
    return AgentEvent(
        "assistant.message.completed",
        turn_id,
        step,
        {"content": content, "tool_calls": tool_calls},
    ).to_dict()


def assistant_message_interrupted_event(
    *,
    turn_id: str,
    step: int,
    content: str,
    reason: str = "aborted",
) -> AgentEventDict:
    return AgentEvent(
        "assistant.message.interrupted",
        turn_id,
        step,
        {"content": content, "reason": reason},
    ).to_dict()


def assistant_thinking_completed_event(
    *,
    turn_id: str,
    step: int,
    content: str,
) -> AgentEventDict:
    return AgentEvent(
        "assistant.thinking.completed",
        turn_id,
        step,
        {"content": content},
    ).to_dict()


def turn_terminal_event(
    *,
    turn_id: str,
    terminal_type: str,
    steps: int,
    error: str = "",
) -> AgentEventDict:
    if terminal_type not in TURN_TERMINAL_TYPES:
        raise ValueError(f"Unknown turn terminal type: {terminal_type}")
    data: dict[str, Any] = {"steps": steps}
    if error:
        data["error"] = str(error)
    return AgentEvent(terminal_type, turn_id, None, data).to_dict()


TOOL_CALL_APPROVAL_PHASES = frozenset({"waiting_approval", "approved", "denied"})


def tool_call_started_event(
    *,
    turn_id: str,
    step: int,
    tool_call_id: str,
    tool_name: str,
    args: dict[str, Any],
    child_turn_id: str = "",
) -> AgentEventDict:
    data: dict[str, Any] = {
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "args": args,
    }
    if child_turn_id:
        data["child_turn_id"] = child_turn_id
    return AgentEvent("tool.call.started", turn_id, step, data).to_dict()


def tool_call_completed_event(
    *,
    turn_id: str,
    step: int,
    tool_call_id: str,
    tool_name: str,
    is_error: bool,
) -> AgentEventDict:
    return AgentEvent(
        "tool.call.completed",
        turn_id,
        step,
        {"tool_call_id": tool_call_id, "tool_name": tool_name, "is_error": is_error},
    ).to_dict()


def tool_call_cancelled_event(
    *,
    turn_id: str,
    step: int,
    tool_call_id: str,
    tool_name: str,
    reason: str = "tool_call_cancelled",
) -> AgentEventDict:
    return AgentEvent(
        "tool.call.cancelled",
        turn_id,
        step,
        {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "reason": reason,
        },
    ).to_dict()


def tool_call_child_started_event(
    *,
    turn_id: str,
    tool_call_id: str,
    child_turn_id: str,
    child_session_id: str,
    child_agent_id: str,
    child_kind: str = "",
) -> AgentEventDict:
    data: dict[str, Any] = {
        "tool_call_id": tool_call_id,
        "child_turn_id": child_turn_id,
        "child_session_id": child_session_id,
        "child_agent_id": child_agent_id,
    }
    if child_kind:
        data["child_kind"] = child_kind
    return AgentEvent("tool.call.child_started", turn_id, None, data).to_dict()


def tool_call_approval_event(
    *,
    turn_id: str,
    phase: str,
    tool_call_id: str,
    tool_name: str,
    step: int | None = None,
    reason: str = "",
    feedback: str = "",
    scope: str = "",
    modified_args: dict[str, Any] | None = None,
) -> AgentEventDict:
    if phase not in TOOL_CALL_APPROVAL_PHASES:
        raise ValueError(f"Unknown tool-call approval phase: {phase}")
    data: dict[str, Any] = {"tool_call_id": tool_call_id, "tool_name": tool_name}
    if reason:
        data["reason"] = reason
    if feedback:
        data["feedback"] = feedback
    if scope:
        data["scope"] = scope
    if modified_args is not None:
        data["modified_args"] = modified_args
    return AgentEvent(f"tool.call.{phase}", turn_id, step, data).to_dict()


def tool_call_approval_request_event(
    *,
    turn_id: str,
    request_id: str,
    tool_call_id: str,
    tool_name: str,
    action: str,
    detail: Any = None,
    step: int | None = None,
) -> AgentEventDict:
    """Durable, replayable approval request.

    Unlike the ``waiting_approval`` phase event (which carries only ids), this
    carries the ``request_id`` + ``action`` + ``detail`` needed to *render and
    answer* the approval. It is published into the turn's live stream and
    persisted to run_events, so any connection that attaches (or reconnects)
    replays the pending approval from the snapshot and can resolve it by id.
    """
    return AgentEvent(
        "tool.call.approval_request",
        turn_id,
        step,
        {
            "request_id": request_id,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "action": action,
            "detail": detail,
        },
    ).to_dict()


def tool_result_event(
    *,
    turn_id: str,
    step: int,
    tool_call_id: str,
    tool_name: str,
    args: dict[str, Any],
    is_error: bool,
    output: object,
    error: str = "",
    retryable: bool = False,
    policy_audit: dict[str, Any] | None = None,
) -> AgentEventDict:
    return ToolResultEvent(
        turn_id=turn_id,
        step=step,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        args=args,
        is_error=is_error,
        output=output,
        error=error,
        retryable=retryable,
        policy_audit=policy_audit,
    ).to_dict()
