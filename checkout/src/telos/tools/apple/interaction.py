"""个人版结构化交互：部分草稿、字段追问与唯一的操作预览。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from telos.adapters.question_channel import QuestionChannel, QuestionRequest
from telos.agent.tools.types import ExecutableTool, ToolExecution, ToolInvocationContext, ToolResult, ToolInputError
from telos.connectors.apple.native import AppleNativeError
from telos.core.jsonschema import validate_json_schema
from telos.core.clock import user_now_iso
from telos.core.ids import generate_id
from telos.tools.apple.drafts import materialize_draft
from telos.tools.apple.repair import is_input_error, payload
from telos.tools.apple.service import AppleService
from telos.tools.apple.tools import create_apple_tool


from telos.tools.apple.tags import validate_tags
from telos.tools.apple.features import QUERY_FIELDS

NAME = "personalPlan"
_ACTIONS = ("query", "detail", "create", "update", "delete")
_FIELD_REQUIREMENTS = {
    "title": "标题", "date": "明确日期", "due": "到期日期", "due_time": "到期日期中的提醒时刻",
    "start": "开始时间", "end": "结束时间", "calendar_id": "目标清单或日历",
    "id": "要修改、删除或读取的对象", "account_id": "提醒事项账户",
    "image_date": "截图中相对日期的基准日期", "timezone": "时间所属时区",
    "intent": "用户期望执行的操作", "image": "清晰可辨认的图片内容", "fields": "需要修改的内容",
    "early_reminder": "提醒事项仅支持到期通知加一次提前提醒；多个提前提醒需要用户选择保留哪一个，或改用日程。",
    "recurrence_scope": "重复日程明确 this/future；重复提醒事项规则修改或删除明确 series。",
    "location": "城市、完整地址及同名地点选择",
}

_WRITE_LABELS = {"create": "创建", "update": "修改", "delete": "删除", "batch": "批量操作"}


def create_personal_plan_tool(*, data_dir: Path, options: dict[str, Any],
                              service: AppleService | None = None, draft_lookup=None) -> ExecutableTool:
    service = service or AppleService.from_options(data_dir, options)
    tools = {action: create_apple_tool(f"apple.{action}", data_dir=data_dir, options=options,
                                     service=service) for action in _ACTIONS}
    properties = {}
    for tool in tools.values():
        properties.update(tool.parameters["properties"])
    properties["fields"] = {key: value for key, value in properties["fields"].items() if key != "minProperties"}
    properties.update(
        action={"type": "string", "enum": [*_ACTIONS, "batch", "revise"]},
        draft_call_id={"type": "string", "description": "revise 必填：当前草稿的工具调用编号。仅提交要修改的字段；程序保留其余字段。"},
        unresolved={"type": "array", "items": {"type": "string", "enum": list(_FIELD_REQUIREMENTS)},
                    "uniqueItems": True,
                    "description": "Apple 草稿尚未明确的信息；截图相对日期不明用 image_date；不确定目标用 calendar_id/id；多个提前提醒限制用 early_reminder。不能用此字段索取操作同意。"},
    )
    properties["kind"]["description"] = "对象类型。create/update/delete 必填：日程 event、事项 reminder、清单 list、群组 group、清单内分区 section、日历 event_calendar；query 可省略表示全部。"
    properties["text"]["description"] = "query 时为搜索词。"
    operation_properties = {key: value for key, value in properties.items()
                            if key not in {"text", "include_completed", "offset", "limit", "draft_call_id"} | QUERY_FIELDS.keys()}
    operation_properties["action"] = {"type": "string", "enum": ["create", "update", "delete"]}
    properties["operations"] = {"type": "array", "minItems": 1, "maxItems": 20,
        "description": "batch 使用完整的多项草稿；在同一预览中逐项列出，一次批准，按顺序保存。",
        "items": {"type": "object", "properties": operation_properties,
                  "required": ["action"], "additionalProperties": False}}

    schema = {"type": "object", "properties": properties, "required": ["action"],
              "additionalProperties": False}

    async def guarded_resolver(args: dict[str, Any], context: ToolInvocationContext) -> ToolExecution:
        try:
            return await resolver({k: v for k, v in args.items() if k not in {"request_mode"}}, context)
        except ToolInputError:
            raise
        except AppleNativeError as error:
            if error.code == "conflict":
                raise ToolInputError(str(error), code="conflict") from error
            raise
        except ValueError as error:
            # 此 resolver 只准备草稿及只读状态；写入仅在返回的 execution.run 内。
            raise ToolInputError(str(error)) from error

    async def resolver(args: dict[str, Any], context: ToolInvocationContext) -> ToolExecution:
        action = args["action"]
        if action not in {"query", "detail"}:
            status = await asyncio.to_thread(service.status)
            if status["uncertain_writes"]:
                return ToolExecution(execute=lambda: {"status": "write_uncertain"})
        if action == "revise":
            def lookup(call_id):
                if not context.session_id:
                    raise ValueError("草稿引用缺少会话编号。")
                record = draft_lookup(context.session_id, call_id) if draft_lookup else None
                if record is None or record.tool_name != NAME:
                    raise ToolInputError("当前会话中找不到该操作草稿，请重新明确目标。", code="invalid_draft_reference")
                if (payload(record.result) or {}).get("saved"):
                    raise ToolInputError("该操作已保存，请读取 Apple 对象后使用 update，不能再次创建。", code="saved_draft")
                if record.is_error and not is_input_error(record.result):
                    raise ToolInputError("该草稿已失败或取消，可能包含已完成的操作。请先查询核实，不能直接重放整份草稿。",
                                         code="executed_draft")
                return {k: v for k, v in record.args.items() if k not in {"request_mode"}}
            args = materialize_draft(args, lookup)
            error = validate_json_schema(schema, args)
            if error:
                raise ToolInputError(error)
            action = args["action"]
        elif "draft_call_id" in args:
            raise ValueError("草稿引用仅用于 revise。")
        if action == "batch":
            if not context.tool_call_id:
                raise ValueError("Apple 写入缺少持久化工具调用编号。")
            if not args.get("operations") or set(args) - {"action", "operations"}:
                raise ValueError("批量操作必须仅包含 action 和 operations。")
            executions = []
            for index, operation in enumerate(args["operations"]):
                execution = await resolver(operation, replace(context,
                    tool_call_id=f"{context.tool_call_id}:{index + 1}"))
                if not execution.need_confirm:
                    async def complete_draft(execution=execution, index=index):
                        result = await execution.run()
                        return {**result, "draft": args, "draft_call_id": context.tool_call_id,
                                "operation_index": index + 1}
                    return replace(execution, execute=complete_draft)
                executions.append(execution)

            async def save_batch():
                completed = []
                for execution in executions:
                    try:
                        completed.append(await execution.run())
                    except Exception as error:
                        return ToolResult(output={"status": "failed", "completed": len(completed), "results": completed,
                            "reason": str(error)}, is_error=True)
                return {"saved": True, "results": completed, "draft": args}
            summaries = [f"{i + 1}. {e.description}\n{e.approval_preview_value()['summary']}"
                         for i, e in enumerate(executions)]
            return ToolExecution(need_confirm=True, description="批量 Apple 操作",
                approval_preview={"action": "batch", "operations": args["operations"],
                                  "summary": "\n\n".join(summaries)}, execute=save_batch)
        draft = {key: value for key, value in args.items() if key not in {"action", "unresolved"}}
        missing = list(args.get("unresolved", []))
        if action in {"query", "detail"}:
            if action == "detail" and not draft.get("id"):
                missing.append("id")
        else:
            kind = draft.get("kind")
            if kind not in {"event", "reminder", "list", "group", "section", "event_calendar"}:
                raise ValueError("操作草稿必须指定对象类型。")
            fields = dict(draft.get("fields", {}))
            validate_tags(kind, fields)
            if kind == "reminder" and isinstance(fields.get("location"), str):
                async def find_location():
                    result = await asyncio.to_thread(service.search_locations, fields["location"])
                    return {**result, "draft": {"action": action, **draft},
                            "draft_call_id": context.tool_call_id}
                return ToolExecution(execute=find_location)
            if "alarm_minutes_before" in fields and kind != "event":
                raise ValueError("提前提醒分钟数仅适用于日程。")
            if "early_reminder_minutes" in fields and kind != "reminder":
                raise ValueError("提前事项提醒仅适用于提醒事项。")
            effective = fields
            if kind == "reminder" and action == "update" and draft.get("id") and draft.get("expected_revision"):
                current = await asyncio.to_thread(service.detail, kind, draft["id"])
                effective = {**current, **fields}
            if kind == "reminder" and effective.get("early_reminder_minutes") is not None and (
                "early_reminder_minutes" in fields or "due" in fields
            ):
                due = effective.get("due")
                if not due:
                    missing.append("due")
                elif due:
                    if len(due) == 10:
                        missing.append("due_time")
                    elif datetime.fromisoformat(due).tzinfo is None:
                        missing.append("timezone")
            if fields.get("recurrence") is not None and kind == "reminder" and not effective.get("due"):
                missing.append("due")
            if action != "create" and draft.get("id") and draft.get("expected_revision"):
                current = await asyncio.to_thread(service.detail, kind, draft["id"],
                    start=draft.get("start", ""), end=draft.get("end", ""))
                if current.get("recurring") and not draft.get("recurrence_scope") and (
                    kind == "event" or action == "delete" or "recurrence" in fields
                ):
                    missing.append("recurrence_scope")
            draft["fields"] = fields
            if action == "create":
                if not fields.get("title", "").strip():
                    missing.append("title")
                if kind == "section" and not fields.get("calendar_id"):
                    missing.append("calendar_id")
                if kind == "event":
                    missing.extend(key for key in ("start", "end") if not fields.get(key))
                    for key in ("start", "end"):
                        if fields.get(key):
                            parsed = datetime.fromisoformat(fields[key])
                            if not fields.get("all_day") and parsed.tzinfo is None:
                                missing.append("timezone")
                    if fields.get("start") and fields.get("end") and not missing:
                        if datetime.fromisoformat(fields["end"]) <= datetime.fromisoformat(fields["start"]):
                            raise ValueError("日程结束时间必须晚于开始时间。")
                if kind in {"event", "reminder"} and not fields.get("calendar_id") and "calendar_id" not in missing:
                    snapshot = await asyncio.to_thread(service.query, {"kind": kind, "limit": 1})
                    candidates = [c for c in snapshot["calendars"] if c["kind"] == kind and c.get("writable")]
                    preferred = [c for c in candidates if kind == "event" and c["title"] == "Telos-日程"]
                    choices = preferred if len(preferred) == 1 else candidates
                    if len(choices) == 1:
                        fields["calendar_id"] = choices[0]["id"]
                    else:
                        missing.append("calendar_id")
                if kind in {"list", "group"} and not fields.get("account_id") and "account_id" not in missing:
                    snapshot = await asyncio.to_thread(service.query, {"kind": kind, "limit": 1})
                    capabilities = snapshot["container_capabilities"]
                    accounts = [a for a in capabilities.get("accounts", []) if a.get("writable")]
                    account = capabilities.get("default_account_id") or (accounts[0]["id"] if len(accounts) == 1 else "")
                    if account:
                        fields["account_id"] = account
                    else:
                        missing.append("account_id")
            else:
                if not draft.get("id"):
                    missing.append("id")
                if action == "update" and not fields:
                    missing.append("fields")
                if not missing and not draft.get("expected_revision"):
                    async def read_baseline():
                        item = await asyncio.to_thread(service.detail, kind, draft["id"],
                            start=draft.get("start", ""), end=draft.get("end", ""))
                        return {"status": "baseline", "draft": args, "draft_call_id": context.tool_call_id, "item": item}
                    return ToolExecution(execute=read_baseline)
        if "early_reminder" in missing and draft.get("kind") != "reminder":
            raise ValueError("多个提前提醒的限制只适用于提醒事项；须提交 reminder 部分草稿。")
        if missing:
            missing = list(dict.fromkeys(missing))
            operation_draft = ({"draft": {"action": action, **draft},
                                "draft_call_id": context.tool_call_id}
                               if action in {"create", "update", "delete"} else {})
            return ToolExecution(execute=lambda: {
                "status": "incomplete_draft", **operation_draft, "missing_fields": missing,
                "requirements": {field: _FIELD_REQUIREMENTS[field] for field in missing},
                "instruction": "先核对原始文字、图片与已核实结果。已有信息用 revise 补齐；确实缺失时调用 ask_user，以 text 提问，不能索取写入批准。"})
        # 同一 ToolExecution 交给 mandatory policy hook；没有内部审批或绕过审批的写入。
        if action == "delete":
            draft.pop("fields", None)
        allowed = tools[action].parameters["properties"]
        unexpected = set(draft) - allowed.keys()
        if unexpected:
            raise ValueError("草稿包含该操作不支持的字段：" + "、".join(sorted(unexpected)))
        execution = await tools[action].resolve_execution(draft, context=context)
        if action not in {"create", "update", "delete"}:
            return execution
        async def save():
            result = await execution.run()
            return {**result, "draft": {"action": action, **draft}}
        return replace(execution, execute=save)

    return ExecutableTool(name=NAME, description=(
        "提交结构化交互结果。query/detail 读取最新 Apple；create/update/delete 提交部分操作草稿。"
        "程序返回缺失字段和约束；先用已有信息补齐，确实缺失时用 ask_user(text) 提问。信息完整后由宿主写入策略决定直接保存或展示预览确认；以返回结果为准。"
        "已有操作草稿使用 revise 和 draft_call_id 提交修改字段，程序合并未修改内容。已保存对象先 query/detail 再 update。普通回复直接使用助手文本。"),
        parameters=schema, resolver=guarded_resolver)


def personal_result_text(args, output):
    """只格式化预览、追问、明确终止和写入回执；普通查询失败留给循环决定后续。"""
    status = output.get("status")
    if status == "awaiting_approval":
        return output["text"]
    if status == "awaiting_input":
        return "需要你回答\n" + output["question"]
    if status == "unavailable" and output.get("question"):
        return output["question"]
    if output.get("saved") is True:
        return "已" + _WRITE_LABELS[output.get("draft", args)["action"]] + "成功。"
    if status == "write_uncertain":
        return "之前的 Apple 操作可能已保存，但结果尚未核实。已暂停新的写入，请先核实已有对象，不要重复创建。"
    if status == "failed" and "completed" in output:
        return f"已完成 {output['completed']} 项，后续操作未完成。请先核实 Apple 的当前状态。"
    if status in {"cancelled", "timeout", "deny"}:
        return "信息补充已结束，本次没有执行操作。"
    if status == "tool_error" and args.get("action") in {"create", "update", "delete", "batch", "revise"}:
        message = output.get("message", "")
        for prefix in (f'Tool "{NAME}" denied: ', f'Tool "{NAME}" failed to prepare: '):
            if message.startswith(prefix):
                reason = message.removeprefix(prefix)
                return "该操作未执行。" + ("" if reason.startswith(("denied by", "cancelled by")) else reason)
        return "该操作未执行。请先核实 Apple 当前状态，不能自动重试。"
    return ""



def create_personal_question_tool(question_channel):
    async def resolve(args, context):
        if not str(args.get("text", "")).strip() or set(args) - {"text", "unresolved"}:
            raise ToolInputError("ask_user 需要 text 中的具体问题，可附带 unresolved 字段；不接受写入参数或操作批准。")
        question = args["text"].strip()
        async def ask():
            if question_channel is None:
                return {"status": "unavailable", "question": question}
            response = await question_channel.request_question(QuestionRequest(
                request_id=generate_id("ques"), session_id=context.session_id,
                turn_id=context.turn_id, tool_call_id=context.tool_call_id,
                tool_name=context.tool_name or "ask_user", question=question,
                detail={"missing_fields": args.get("unresolved", [])}))
            return {"status": response.decision, "question": question,
                    "missing_fields": args.get("unresolved", []), "answer": response.answer,
                    "time": response.message_time or user_now_iso()}
        return ToolExecution(execute=ask)
    return ExecutableTool(name="ask_user", description="询问确实缺少的信息；不用于批准 Apple 写入。待回答时可以继续独立任务。",
        parameters={"type": "object", "properties": {
            "text": {"type": "string", "minLength": 1},
            "unresolved": {"type": "array", "items": {"type": "string", "enum": list(_FIELD_REQUIREMENTS)}, "uniqueItems": True}},
            "required": ["text"], "additionalProperties": False}, resolver=resolve)

