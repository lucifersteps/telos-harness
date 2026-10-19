"""Model tools using Telos' mandatory policy hook for all Apple writes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from telos.agent.tools.types import ExecutableTool, ToolExecution, ToolInvocationContext
from telos.core.clock import USER_TIMEZONE
from telos.tools.apple.service import AppleService, operation_key
from telos.tools.apple.locations import LOCATION_SCHEMA, location_summary
from telos.tools.apple.recurrence import RECURRENCE_SCHEMA, recurrence_summary, validate_recurrence, validate_recurrence_scope

FIELDS = {
    "recurrence": RECURRENCE_SCHEMA,
    "flagged": {"type": "boolean", "description": "仅提醒事项：设置或取消旗标，与优先级独立。"},
    "title": {"type": "string", "minLength": 1}, "notes": {"type": "string"},
    "url": {"type": "string"}, "calendar_id": {"type": "string"},
    "due": {"type": "string", "description": "提醒事项到期日：YYYY-MM-DD 或带时区的 ISO 8601；空串清除。"},
    "completed": {"type": "boolean"}, "priority": {"type": "integer", "minimum": 0, "maximum": 9},
    "start": {"type": "string", "description": "日程开始时间，带时区的 ISO 8601。"},
    "end": {"type": "string", "description": "日程结束时间，带时区的 ISO 8601。"},
    "all_day": {"type": "boolean"}, "location": LOCATION_SCHEMA,
    "alarm_minutes_before": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 525600},
        "uniqueItems": True,
        "description": "仅日程：距 start 的完整提醒分钟数列表，可设置多个不同日期/时刻。全天 start 为零点，提前14天和7天各上午9点用[19620,9540]（天数×1440−540）；[20160,10080]是那两天零点，[40]为提前40分钟。保留活动起止时间。列表替换原有提醒，[]清除，省略保留。"},
    "early_reminder_minutes": {"type": ["integer", "null"], "minimum": 1, "maximum": 525600,
        "description": "仅提醒事项：一个提前提醒，距到期时间的分钟数；10080为提前7天。保留到期日期；null仅清除提前提醒，省略保留。须有明确到期日期和时刻。"},
    "account_id": {"type": "string", "description": "创建清单或群组所用账户，来自查询结果。群组必填；清单可用默认提醒事项账户。"},
    "group_id": {"type": "string", "description": "清单所属群组编号；空串移回账户顶层。只适用于清单。"},
    "color": {"type": "string", "description": "清单颜色：#RRGGBB，或 red/orange/yellow/green/blue/purple/gray/brown/pink。"},
    "emoji": {"type": "string", "description": "清单图标：一个 emoji。与 symbol 互斥。"},
    "symbol": {"type": "string", "description": "原生清单图标，必须使用查询返回的 symbols 编号；default 恢复默认图标。与 emoji 互斥。"},
}


def container_summary(action: str, args: dict, snapshot: dict, current: dict | None) -> str:
    values = {**(current or {}), **args.get("fields", {})}
    fields = args.get("fields", {})
    containers = snapshot["containers"]
    names = {item["id"]: item["title"] for item in containers}
    kind = "清单" if args["kind"] == "list" else "群组"
    lines = [f"{kind}：{values.get('title', '')}"]
    if action == "update":
        lines.append("当前名称：" + str((current or {}).get("title", "")))
    if "color" in fields:
        labels = {"red": "红色", "orange": "橙色", "yellow": "黄色", "green": "绿色", "blue": "蓝色",
                  "purple": "紫色", "gray": "灰色", "brown": "棕色", "pink": "粉色"}
        color = fields["color"]
        lines.append("颜色：" + labels.get(color.lower(), color))
    if "emoji" in fields or "symbol" in fields:
        icon = str(fields.get("emoji", fields.get("symbol", "")))
        lines.append("图标：" + {"default": "默认列表", "concept3": "灯泡", "sport2": "跑步"}.get(icon, icon))
    if "group_id" in fields:
        lines.append("归属：" + (names[fields["group_id"]] if fields["group_id"] else "账户顶层"))
    if action == "create":
        account_id = values.get("account_id", "")
        accounts = snapshot["container_capabilities"].get("accounts", [])
        lines.append("账户：" + next((a["title"] for a in accounts if a["id"] == account_id), "默认提醒事项账户"))
    if action == "delete":
        if args["kind"] == "group":
            children = (current or {}).get("child_list_ids", [])
            lines.append("保留其中的清单和任务，清单移回账户顶层：" + ("、".join(names[i] for i in children) or "当前为空"))
        else:
            lines.append("只允许删除空清单；包含已完成任务也不能删除。")
    return "\n".join(lines)


def _format_preview_time(value: object) -> str:
    """仅格式化操作摘要，带时区时间转为北京时间，纯日期不补时分。"""
    raw = str(value or "")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(USER_TIMEZONE)
    pattern = "%Y年%m月%d日" if len(raw) == 10 else "%Y年%m月%d日 %H:%M"
    return parsed.strftime(pattern)


def write_summary(action: str, args: dict[str, Any], calendars: list[dict], current: dict | None) -> str:
    fields = args.get("fields", {})
    values = {**(current or {}), **fields}
    calendar_id = values.get("calendar_id", "")
    calendar = next((c["title"] for c in calendars if c["id"] == calendar_id), "目标清单/日历不可用")
    lines = [f"标题：{str(values.get('title', ''))[:180]}"]
    if "recurrence" in fields:
        lines.append("重复：" + recurrence_summary(fields["recurrence"]))
    elif values.get("recurrence_rules"):
        lines.extend("当前重复：" + recurrence_summary(rule) for rule in values["recurrence_rules"])
    if args.get("recurrence_scope"):
        lines.append("操作范围：" + {"this": "仅本次日程", "future": "本次及以后的日程（此前不变）", "series": "整条重复提醒事项"}[args["recurrence_scope"]])
    if args.get("recurrence_scope") == "future" and "recurrence" in fields:
        lines.append("重新设置重复规则会重建本次及以后的实例，可能覆盖此前单独修改的未来实例。")
    if args["kind"] == "reminder" and ("flagged" in fields or values.get("flagged")):
        lines.append("旗标：" + ("已标记" if values.get("flagged") else "取消标记"))
    if args["kind"] == "reminder" and ("location" in fields or values.get("location")):
        lines.extend(location_summary(values.get("location")))
        if values.get("location") and values.get("due"):
            lines.append("触发条件：到期时间或地点条件，先满足者提醒；不是同时满足。")
    if args["kind"] == "event":
        for name, label in [("start", "开始"), ("end", "结束")]:
            lines.append(f"{label}：{_format_preview_time(values.get(name))}")
        lines.append("全天：" + ("是" if values.get("all_day") else "否"))
        lines.append("地点：" + str(values.get("location", "未指定"))[:150])
        if "alarm_minutes_before" in fields:
            offsets = fields["alarm_minutes_before"]
            if not offsets:
                lines.append("提醒：清除")
            elif values.get("start"):
                for minutes in sorted(offsets, reverse=True):
                    alarm_time = datetime.fromisoformat(values["start"]) - timedelta(minutes=minutes)
                    lines.append(f"提前提醒：{_format_preview_time(alarm_time.isoformat())}（提前{minutes}分钟）")
            if action == "update" and (current or {}).get("alarms") and offsets:
                lines.append("替换该日程原有提醒。")
    else:
        lines.append("到期：" + (_format_preview_time(values.get("due")) or "无到期日"))
        early = values.get("early_reminder_minutes")
        if early is not None and values.get("due"):
            alarm_time = datetime.fromisoformat(values["due"]) - timedelta(minutes=early)
            lines.append(f"提前提醒：{_format_preview_time(alarm_time.isoformat())}（提前{early}分钟）")
            lines.append("保留到期提醒。")
        elif "early_reminder_minutes" in fields:
            lines.append("提前提醒：清除，保留到期提醒。")
        lines.append("状态：" + ("已完成" if values.get("completed") else "未完成"))
        if "priority" in fields:
            lines.append("优先级：" + str(fields["priority"]))
    lines.append("日历：" + calendar if args["kind"] == "event" else "清单：" + calendar)
    if action == "update":
        labels = {"recurrence": "重复规则", "flagged": "旗标", "title": "标题", "notes": "备注", "url": "链接", "calendar_id": "所属清单/日历",
                  "due": "到期日", "completed": "完成状态", "priority": "优先级", "start": "开始时间",
                  "end": "结束时间", "all_day": "全天", "location": "地点", "alarm_minutes_before": "日程提醒", "early_reminder_minutes": "提前提醒"}
        ordered_fields = [key for key in labels if key in fields] + sorted(fields.keys() - labels.keys())
        lines.append("修改字段：" + "、".join(labels.get(key, key) for key in ordered_fields))
        lines.append("当前标题：" + str((current or {}).get("title", ""))[:180])
    for name, label in [("notes", "备注"), ("url", "链接")]:
        if name in fields:
            lines.append(label + "：" + (str(fields[name])[:180] or "清空"))
    return "\n".join(lines)


def create_apple_tool(backend: str, *, data_dir: Path, options: dict[str, Any], service=None) -> ExecutableTool:
    service = service or AppleService.from_options(data_dir, options)
    action = backend.removeprefix("apple.")
    if action not in {"query", "detail", "create", "update", "delete"}:
        raise ValueError(f"Unknown Apple backend: {backend}")
    names = {"query": "appleQuery", "detail": "appleDetail", "create": "appleCreate",
             "update": "appleUpdate", "delete": "appleDelete"}
    descriptions = {
        "query": "重新读取 Apple。kind=reminder/event 查事项，list/group 查清单/群组；返回归属、外观、账户和原生图标目录及 revision。默认日历范围过去30天到未来180天。",
        "detail": "重新读取 Apple 事项、清单或群组及 revision；id 来自 appleQuery。",
        "create": "创建 Apple 事项、清单或群组。事项必填 calendar_id，日程必填 start/end；清单和群组必填 title，可指定账户。清单支持颜色、图标和群组归属。用户指令明确且信息齐全时直接调用；工具负责预览和唯一一次确认，不先发送创建前确认。",
        "update": "修改 Apple 对象，仅提交要改的字段。清单支持名称、颜色、emoji 或原生 symbol 图标、group_id 归属；群组支持改名。必须带 expected_revision。直接调用进入唯一一次工具审批，不先索取普通消息确认；批准后检查版本。",
        "delete": "删除 Apple 对象，必须带 expected_revision。直接调用进入唯一一次工具审批，不先索取普通消息确认。清单仅可删除空清单；删除群组保留子清单和任务。重复日程必须指定this/future范围；删除重复提醒事项须指定series。",
    }
    properties: dict[str, Any] = {
        "kind": {"type": "string", "enum": ["reminder", "event", "list", "group"]},
        "start": {"type": "string", "description": "可选日历查询窗口开始，带时区的 ISO 8601。"},
        "end": {"type": "string", "description": "可选日历查询窗口结束，带时区的 ISO 8601。"},
    }
    required = []
    if action == "query":
        properties.update(text={"type": "string"}, include_completed={"type": "boolean"},
                          offset={"type": "integer", "minimum": 0},
                          limit={"type": "integer", "minimum": 1, "maximum": 500})
    else:
        required = ["kind"]
    if action in {"detail", "update", "delete"}:
        properties["id"] = {"type": "string", "minLength": 1}
        required.append("id")
    if action in {"update", "delete"}:
        properties["recurrence_scope"] = {"type": "string", "enum": ["this", "future", "series"],
            "description": "重复日程修改/删除必须明确this仅本次或future本次及以后；更改重复规则只能future。重复提醒事项改规则或删除需series整条。不能猜测范围。"}
        properties["expected_revision"] = {"type": "string", "minLength": 64, "maxLength": 64}
        required.append("expected_revision")
    if action in {"create", "update"}:
        properties["fields"] = {"type": "object", "properties": FIELDS,
                                "additionalProperties": False, "minProperties": 1}
        required.append("fields")

    async def resolver(args: dict[str, Any], context: ToolInvocationContext) -> ToolExecution:
        if action == "query":
            return ToolExecution(execute=lambda: asyncio.to_thread(service.query, args))
        if action == "detail":
            return ToolExecution(execute=lambda: asyncio.to_thread(
                service.detail, args["kind"], args["id"], start=args.get("start", ""), end=args.get("end", "")))
        if not context.tool_call_id:
            raise ValueError("Apple 写入缺少持久化工具调用编号。")
        operation_id = operation_key(context.session_id, context.turn_id, context.tool_call_id)
        preview = {"action": action, **args}
        if action != "create":
            preview["current"] = await asyncio.to_thread(
                service.detail, args["kind"], args["id"], start=args.get("start", ""), end=args.get("end", ""))
            from telos.connectors.apple.native import AppleNativeError
            if preview["current"]["revision"] != args["expected_revision"]:
                raise AppleNativeError("对象已被修改，请重新查询并确认。", code="conflict")
        fields = args.get("fields", {})
        current = preview.get("current", {})
        validate_recurrence_scope(args["kind"], action, current, fields, args.get("recurrence_scope"))
        if args["kind"] == "reminder" and "due" in fields and not fields["due"] and current.get("recurring") and not ("recurrence" in fields and fields["recurrence"] is None):
            raise ValueError("取消到期日期前须同时取消重复规则。")
        if "flagged" in fields and args["kind"] != "reminder":
            raise ValueError("旗标仅适用于提醒事项。")
        if "recurrence" in fields:
            if args["kind"] not in {"reminder", "event"}:
                raise ValueError("重复规则仅适用于事项和日程。")
            values = {**current, **fields}
            anchor = values.get("due" if args["kind"] == "reminder" else "start")
            validate_recurrence(fields["recurrence"], anchor)
            if fields["recurrence"] is not None and not anchor:
                raise ValueError("重复规则需要首次日期。")
        snapshot = await asyncio.to_thread(service.query, {"kind": args["kind"], "limit": 1,
            **{key: args[key] for key in ["start", "end"] if args.get(key)}})
        approved_args = {**args, "fields": dict(args.get("fields", {}))}
        if args["kind"] in {"list", "group"}:
            capabilities = snapshot["container_capabilities"]
            fields = approved_args["fields"]
            if action == "create":
                accounts = [a for a in capabilities.get("accounts", []) if a.get("writable")]
                if not fields.get("account_id"):
                    fields["account_id"] = capabilities.get("default_account_id") or (accounts[0]["id"] if len(accounts) == 1 else "")
                if not any(a["id"] == fields["account_id"] for a in accounts):
                    raise ValueError("无法确定可写账户，请重新查询并明确目标账户。")
            if action in {"create", "delete"} or args["kind"] == "group" or "group_id" in fields:
                if capabilities.get("restricted_scope"):
                    raise ValueError("限制清单同步范围时不支持结构管理，请先调整同步范围。")
            if fields.get("group_id"):
                destination = next((c for c in snapshot["containers"] if c["id"] == fields["group_id"] and c["kind"] == "group"), None)
                if destination is None:
                    raise ValueError("目标群组不可用，请重新查询。")
                approved_args["expected_destination"] = {key: value for key, value in destination.items() if key != "revision"}
            preview["summary"] = container_summary(action, approved_args, snapshot, preview.get("current"))
            preview["fields"] = fields
        else:
            fields = args.get("fields", {})
            if "location" in fields:
                if args["kind"] == "reminder":
                    location = await asyncio.to_thread(service.resolve_location, fields["location"])
                    approved_args["fields"]["location"] = location
                    preview["fields"] = approved_args["fields"]
                elif not isinstance(fields["location"], str):
                    raise ValueError("日程地点必须是文字；到达或离开提醒仅适用于提醒事项。")
            if "early_reminder_minutes" in fields and args["kind"] != "reminder":
                raise ValueError("提前事项提醒仅适用于提醒事项。")
            if "alarm_minutes_before" in fields:
                offsets = fields["alarm_minutes_before"]
                if args["kind"] != "event" or len(offsets) != len(set(offsets)):
                    raise ValueError("日程提醒仅适用于日程，且时间不能重复。")
                calendar_id = fields.get("calendar_id", (preview.get("current") or {}).get("calendar_id"))
                calendar = next((c for c in snapshot["calendars"] if c["id"] == calendar_id), {})
                if calendar.get("max_alarms") is not None and len(offsets) > calendar["max_alarms"]:
                    raise ValueError("目标日历不支持这么多提醒，请减少提醒数量。")
            values = {**(preview.get("current") or {}), **fields}
            if args["kind"] == "reminder" and values.get("early_reminder_minutes") is not None and (
                "early_reminder_minutes" in fields or "due" in fields
            ):
                due = str(values.get("due", ""))
                if len(due) <= 10 or datetime.fromisoformat(due).tzinfo is None:
                    raise ValueError("设置提前提醒需要明确到期日期、时刻和时区。")
            preview["summary"] = write_summary(action, approved_args, snapshot["calendars"], preview.get("current"))
        return ToolExecution(need_confirm=True, approval_preview=preview,
                             description={"create": "创建", "update": "修改", "delete": "删除"}[action]
                                 + {"event": " Apple 日程", "reminder": " Apple 提醒事项", "list": " Apple 清单", "group": " Apple 群组"}[args["kind"]],
                             execute=lambda: asyncio.to_thread(service.mutate, action, approved_args, operation_id))

    return ExecutableTool(name=names[action], description=descriptions[action],
                          parameters={"type": "object", "properties": properties, "required": required,
                                      "additionalProperties": False}, resolver=resolver)
