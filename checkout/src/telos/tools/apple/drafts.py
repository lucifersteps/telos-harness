"""从已有工具记录恢复草稿；补丁只修改明确提交的字段，不新增持久化状态。"""

from copy import deepcopy
from typing import Callable


def materialize_draft(args: dict, lookup: Callable[[str], dict], seen: frozenset[str] = frozenset()) -> dict:
    if args.get("action") != "revise":
        if args.get("action") not in {"create", "update", "delete", "batch"}:
            raise ValueError("只能修改已有操作草稿。")
        return deepcopy(args)
    reference = str(args.get("draft_call_id", ""))
    if not reference or reference in seen or len(seen) >= 64:
        raise ValueError("草稿引用缺失或形成循环。")
    if set(args) - {"action", "draft_call_id", "kind", "fields", "unresolved", "expected_revision", "operations", "recurrence_scope"}:
        raise ValueError("修改草稿仅接受引用、对象类型、修改字段、版本或批量子项。")
    base = materialize_draft(lookup(reference), lookup, seen | {reference})
    if base["action"] == "batch":
        if set(args) - {"action", "draft_call_id", "operations"} or not args.get("operations"):
            raise ValueError("修改批量草稿必须提交完整 operations。")
        return {"action": "batch", "operations": deepcopy(args["operations"])}
    if "operations" in args:
        raise ValueError("单项草稿不能转成批量操作。")
    kind = args.get("kind", base.get("kind"))
    fields = dict(base.get("fields", {}))
    if kind != base.get("kind"):
        if base["action"] != "create" or {kind, base.get("kind")} != {"event", "reminder"}:
            raise ValueError("只能在未保存的日程与提醒事项草稿之间切换类型。")
        # 清单与日历不同，旧类型的时间和提醒字段也不能带到新类型。
        for key in ("calendar_id", "due", "early_reminder_minutes", "completed", "priority",
                    "start", "end", "all_day", "location", "alarm_minutes_before", "flagged", "recurrence", "tags", "parent_id", "section_id", "urgent", "attachments", "travel_time_minutes"):
            fields.pop(key, None)
    fields.update(deepcopy(args.get("fields", {})))
    base.update(kind=kind, fields=fields, unresolved=list(args.get("unresolved", [])))
    if "recurrence_scope" in args:
        base["recurrence_scope"] = args["recurrence_scope"]
    if "expected_revision" in args:
        base["expected_revision"] = args["expected_revision"]
    return base
