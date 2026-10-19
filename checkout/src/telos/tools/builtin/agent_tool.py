"""Agent collaboration tool."""

from __future__ import annotations

from telos.agent.subagents.host import SubagentHost
from telos.agent.tools.types import (
    ExecutableTool,
    ToolInvocationContext,
    create_executable_tool,
)


def create_agent_tool(host: SubagentHost) -> ExecutableTool:
    profiles = host.available_profiles()
    return create_executable_tool(
        name="agent",
        description="启动或恢复一个 Telos 子代理，并返回它的总结。",
        parameters={
            "type": "object",
            "properties": {
                "name": _profile_name_schema(host, profiles),
                "resume": {
                    "type": "string",
                    "description": (
                        "要恢复的已有子代理 id。继续之前的子代理对话时使用它，而不是 name。"
                    ),
                },
                "prompt": {
                    "type": "string",
                    "description": "发给子代理的任务提示。",
                },
                "reference_turn": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 20,
                    "description": (
                        "要带给子代理的参考聊天记录轮次数。轮次按 root agent 的完整"
                        "运行轮次计算，包含该轮内的 user、assistant 和 tool 消息。"
                    ),
                },
                "background": {
                    "type": "boolean",
                    "description": (
                        "后台运行：立即返回、不等待子代理完成。适合耗时且无需当场用到"
                        "结果的任务。完成后写通知事件，不会回填当前父轮次；父轮次 /stop "
                        "不会取消它，只有『停止整个智能体树』才会。只能与 name 搭配，不能 resume。"
                    ),
                },
            },
            "required": ["prompt"],
            "oneOf": [
                {"type": "object", "required": ["name"]},
                {"type": "object", "required": ["resume"]},
            ],
            "additionalProperties": False,
        },
        context_handler=lambda args, context: _run_agent(host, dict(args), context),
    )


def _profile_name_schema(host: SubagentHost, profiles: list[str]) -> dict:
    description = "子代理名称。"
    summaries = []
    for index, name in enumerate(profiles, start=1):
        profile = host.profiles.get(name)
        summary = f"{index}. {name}"
        profile_description = _normalized_description(
            "" if profile is None else profile.description
        )
        if profile_description:
            summary += f"\ndescription：{profile_description}"
        summaries.append(summary)
    if summaries:
        description += "可用子代理：\n" + "\n".join(summaries)
    schema = {
        "type": "string",
        "description": description,
    }
    if profiles:
        schema["enum"] = profiles
    return schema


def _normalized_description(text: str) -> str:
    return " ".join(str(text or "").split())


async def _run_agent(
    host: SubagentHost,
    args: dict,
    context: ToolInvocationContext,
) -> dict:
    name = str(args.get("name") or "").strip()
    resume = str(args.get("resume") or "").strip()
    prompt = str(args.get("prompt") or "").strip()
    reference_turn = _reference_turn(args)
    background = bool(args.get("background"))
    if not prompt:
        raise ValueError("agent prompt is required")
    if bool(name) == bool(resume):
        raise ValueError("agent requires exactly one of name or resume")
    if background:
        if not name:
            raise ValueError("background subagents require name (resume is not allowed)")
        handle = host.spawn_background(
            profile_name=name,
            prompt=prompt,
            reference_turn=reference_turn,
            source_tool_call_id=context.tool_call_id,
            parent_turn_id=context.turn_id,
        )
        return {
            "agent_id": handle.agent_id,
            "name": handle.profile_name,
            "status": "background_started",
            "background": True,
            "resume_hint": (
                "后台子代理已启动；完成后会以 conversation.subagent.background.* "
                "通知事件返回结果，不会自动回填本轮。"
            ),
        }
    if resume:
        result = await host.resume(
            agent_id=resume,
            prompt=prompt,
            reference_turn=reference_turn,
            source_tool_call_id=context.tool_call_id,
            parent_turn_id=context.turn_id,
        )
    else:
        result = await host.spawn(
            profile_name=name,
            prompt=prompt,
            reference_turn=reference_turn,
            source_tool_call_id=context.tool_call_id,
            parent_turn_id=context.turn_id,
        )
    return {
        "agent_id": result.agent_id,
        "name": result.profile_name,
        "status": result.status,
        "turn_id": result.turn_id,
        "response": result.response,
        "error": result.error,
        "quality_issue": result.quality_issue,
        "remedy_attempted": result.remedy_attempted,
        "started": result.started,
        "resume_hint": (
            f'Use agent(resume="{result.agent_id}", prompt="...") to continue this subagent.'
        ),
    }


def _reference_turn(args: dict) -> int:
    return int(args.get("reference_turn") or 0)
