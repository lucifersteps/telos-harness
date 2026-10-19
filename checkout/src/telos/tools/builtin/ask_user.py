"""Ask the current user a durable question."""

from __future__ import annotations

from typing import Any

from telos.adapters.question_channel import QuestionChannel, QuestionRequest
from telos.agent.tools.types import (
    ExecutableTool,
    ToolInvocationContext,
    ToolResult,
    create_executable_tool,
)
from telos.core.ids import generate_id


def create_ask_user_tool(channel: QuestionChannel) -> ExecutableTool:
    async def _run(args: dict[str, Any], context: ToolInvocationContext) -> ToolResult:
        question = str(args.get("question") or "").strip()
        if not question:
            raise ValueError("ask_user question is required")
        options = _string_list(args.get("options"))
        allow_free_text = bool(args.get("allow_free_text", True))
        timeout_seconds = _timeout_seconds(args.get("timeout_seconds"))
        response = await channel.request_question(
            QuestionRequest(
                request_id=generate_id("ques"),
                session_id=context.session_id,
                turn_id=context.turn_id,
                tool_call_id=context.tool_call_id,
                tool_name=context.tool_name or "ask_user",
                question=question,
                options=options,
                allow_free_text=allow_free_text,
                timeout_seconds=timeout_seconds,
            )
        )
        decision = str(response.decision or "").strip().lower()
        if decision == "answered":
            return ToolResult(
                output={
                    "status": "answered",
                    "answer": response.answer,
                    "selected_options": list(response.selected_options or []),
                }
            )
        status = decision or "cancelled"
        return ToolResult(
            output={
                "status": status,
                "answer": response.answer,
                "selected_options": list(response.selected_options or []),
                "feedback": response.feedback,
            },
            is_error=True,
            error=f"user_question_{status}",
            retryable=status in {"timeout", "cancelled", "unavailable"},
        )

    return create_executable_tool(
        name="ask_user",
        description=(
            "向用户提出一个继续前必须由用户回答的问题。"
            "只在缺少关键信息、需要用户选择，或继续执行会明显偏离用户意图时使用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "要问用户的问题。必须具体、短，并说明你需要用户决定什么。",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选答案列表。没有固定选项时留空。",
                },
                "allow_free_text": {
                    "type": "boolean",
                    "description": "是否允许用户自由输入。默认允许。",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3600,
                    "description": "等待用户回复的秒数，默认 600。",
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        },
        context_handler=lambda args, context: _run(dict(args), context),
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _timeout_seconds(value: object) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = 600.0
    return min(3600.0, max(1.0, seconds))
