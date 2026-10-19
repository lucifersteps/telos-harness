"""Model-facing schedule tools."""

from __future__ import annotations

from typing import Any

from telos.agent.tools.types import ExecutableTool, create_executable_tool
from telos.tools.schedule.service import ScheduleService


def create_schedule_tools(service: ScheduleService) -> list[ExecutableTool]:
    return [
        create_executable_tool(
            name="scheduleCreate",
            description=(
                "使用五段 cron 表达式创建一个 agent 级定时任务。只有当用户希望"
                "按原文发送固定提醒时才使用 executionMode=directMessage；当 Telos 需要在"
                "触发时结合上下文生成最终消息时使用 executionMode=agentTurn。触发时会发到"
                "最近一次真实用户消息所在的 IM。cron 按 Telos 用户时间解析。当前有意不支持"
                "编辑：需要修改时请删除后重建。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "cron": {
                        "type": "string",
                        "minLength": 1,
                        "description": "五段 cron 表达式：分钟 小时 日期 月份 星期。",
                    },
                    "prompt": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "directMessage 模式下这是要发送的精确文本；agentTurn 模式下这是"
                            "定时 agent turn 的指令。"
                        ),
                    },
                    "recurring": {
                        "type": "boolean",
                        "default": True,
                        "description": "是否重复执行；false 表示触发一次后结束。",
                    },
                    "executionMode": {
                        "type": "string",
                        "enum": ["directMessage", "agentTurn"],
                        "default": "agentTurn",
                        "description": (
                            "触发后的执行模式：directMessage 直接发送固定文本；agentTurn "
                            "启动一次 agent 回合生成消息。"
                        ),
                    },
                },
                "required": ["cron", "prompt"],
                "additionalProperties": False,
            },
            handler=lambda args: service.create(
                cron=str(args["cron"]),
                prompt=str(args["prompt"]),
                recurring=bool(args.get("recurring", True)),
                execution_mode=str(args.get("executionMode") or "agentTurn"),
            ).to_public_dict(),
            need_confirm=True,
            approval_preview=lambda args: _create_preview(args),
        ),
        create_executable_tool(
            name="scheduleList",
            description="列出当前 agent 的活跃定时任务。",
            parameters={
                "type": "object",
                "properties": {
                    "includeFailed": {
                        "type": "boolean",
                        "default": True,
                        "description": "是否包含最近执行失败但仍保留的定时任务。",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            handler=lambda args: {
                "tasks": [
                    task.to_public_dict()
                    for task in service.list(
                        include_failed=bool(args.get("includeFailed", True)),
                    )
                ]
            },
        ),
        create_executable_tool(
            name="scheduleDelete",
            description="删除当前 agent 的定时任务。若要修改任务，请删除后创建替代任务。",
            parameters={
                "type": "object",
                "properties": {
                    "taskId": {
                        "type": "string",
                        "minLength": 1,
                        "description": "要删除的定时任务 id。",
                    },
                },
                "required": ["taskId"],
                "additionalProperties": False,
            },
            handler=lambda args: {
                "deleted": service.delete(str(args.get("taskId") or ""))
            },
            need_confirm=True,
            approval_preview=lambda args: {
                "action": "delete schedule",
                "taskId": str(args.get("taskId") or ""),
            },
        ),
    ]


def _create_preview(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": "create schedule",
        "cron": str(args.get("cron") or ""),
        "recurring": bool(args.get("recurring", True)),
        "executionMode": str(args.get("executionMode") or "agentTurn"),
        "prompt": str(args.get("prompt") or "")[:500],
    }
