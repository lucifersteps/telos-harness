"""主 agent 用来委派长期助手的 ``assistant`` 工具。

该工具按 turn 构建，当前用户的 ``ConversationRef``、父会话、审批通道和取消句柄
通过闭包注入 —— 这些都不在 ``ToolInvocationContext`` 上；没有它们，确认透传和
``/stop`` 传播就到不了助手 turn。``reference_turn`` 非 0 时把父会话最近若干轮
聊天渲染进转交文本。
"""

from __future__ import annotations

from telos.adapters.approval_channel import ApprovalChannel
from telos.adapters.base import ConversationRef
from telos.adapters.question_channel import QuestionChannel
from telos.agent.context.recent_context import prompt_with_recent_context
from telos.agent.tools.types import (
    ExecutableTool,
    ToolInvocationContext,
    create_executable_tool,
)
from telos.assistants.service import AssistantService
from telos.runtime.cancel import CancelHandle
from telos.session.session import Session


def create_assistant_tool(
    service: AssistantService,
    *,
    user_ref: ConversationRef,
    parent_session: Session,
    approval_channel: ApprovalChannel | None = None,
    question_channel: QuestionChannel | None = None,
    abort_check: CancelHandle | None = None,
) -> ExecutableTool:
    async def _run(args: dict, context: ToolInvocationContext) -> dict:
        name = str(args.get("name") or "").strip()
        if not name:
            raise ValueError("assistant name is required")
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("assistant prompt is required")
        reference_turn = int(args.get("reference_turn") or 0)
        if reference_turn:
            prompt = prompt_with_recent_context(
                prompt,
                parent_session,
                reference_turn=reference_turn,
                tool_name="assistant",
            )
        result = await service.handle_main_agent_message(
            assistant_id=name,
            user_ref=user_ref,
            message=prompt,
            approval_channel=approval_channel,
            question_channel=question_channel,
            abort_check=abort_check,
            source_tool_call_id=context.tool_call_id,
            parent_turn_id=context.turn_id,
        )
        return {
            "status": result.status,
            "agent_name": result.agent_name,
            "session_id": result.session_id,
            "turn_id": result.turn_id,
            "response": result.response,
        }

    return create_executable_tool(
        name="assistant",
        description=(
            "把任务或问题转交给一个长期助手处理。长期助手拥有独立、持续的对话"
            "上下文；写操作会向用户请求确认。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": _assistant_name_schema(service),
                "prompt": {
                    "type": "string",
                    "description": "转交给助手的任务或问题。",
                },
                "reference_turn": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 20,
                    "description": (
                        "要带给助手的参考聊天记录轮次数。轮次按 root agent 的完整"
                        "运行轮次计算，包含该轮内的 user、assistant 和 tool 消息。"
                    ),
                },
            },
            "required": ["name", "prompt"],
            "additionalProperties": False,
        },
        context_handler=lambda args, context: _run(dict(args), context),
    )


def _assistant_name_schema(service: AssistantService) -> dict:
    description = "助手名称。"
    summaries = []
    for index, definition in enumerate(service.registry.list(), start=1):
        summary = f"{index}. {definition.id}"
        normalized = " ".join(str(definition.description or "").split())
        if normalized:
            summary += f"\ndescription：{normalized}"
        summaries.append(summary)
    if summaries:
        description += "可用助手：\n" + "\n".join(summaries)
    ids = service.registry.ids()
    schema = {
        "type": "string",
        "description": description,
    }
    if ids:
        schema["enum"] = ids
    return schema
