"""Things facade tools."""

from __future__ import annotations

from typing import Any

from telos.agent.tools.types import ExecutableTool, create_executable_tool
from telos.connectors.apple.native import AppleNativeJsonHelperClient
from telos.tools.things.apple_bridge import export_apple_items, import_apple_items
from telos.tools.things.repository import ThingsRepository


THING_TYPES = ["domain", "list", "section", "task", "event"]
ITEM_TYPES = ["task", "event"]
CONTAINER_TYPES = ["domain", "list", "section"]
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def create_things_tools(repository: ThingsRepository) -> list[ExecutableTool]:
    return [
        create_executable_tool(
            name="thingsList",
            description="列出 Telos Things 本地库中的领域、清单、分节、任务和日程。默认返回 status=open 的 task/event 摘要。",
            parameters=_list_schema(),
            handler=lambda args: repository.list(dict(args or {})),
        ),
        create_executable_tool(
            name="thingsDetail",
            description="读取一个或多个 Things 对象的完整详情。",
            parameters={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": _detail_target_schema(),
                        "minItems": 1,
                        "description": "要读取详情的对象列表；优先用 id，按 title 定位时必须唯一。",
                    }
                },
                "required": ["items"],
                "additionalProperties": False,
            },
            handler=lambda args: repository.detail(_dict_list(args.get("items"))),
        ),
        create_executable_tool(
            name="thingsCreate",
            need_confirm=True,
            description="在 Telos Things 本地库中创建领域、清单、分节、任务或日程。",
            parameters={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": _create_item_schema(),
                        "minItems": 1,
                        "description": "要创建的对象数组；每项用 type 区分字段。",
                    }
                },
                "required": ["items"],
                "additionalProperties": False,
            },
            handler=lambda args: repository.create(_dict_list(args.get("items"))),
            approval_preview=lambda args: _write_preview("创建", _dict_list(args.get("items"))),
        ),
        create_executable_tool(
            name="thingsUpdate",
            need_confirm=True,
            description="局部更新 Telos Things 本地库中的对象；不能删除，归档请把 status 设为 archived。",
            parameters={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": _update_item_schema(),
                        "minItems": 1,
                        "description": "要更新的对象数组。省略字段表示不改，传 null 表示清空可清空字段。",
                    }
                },
                "required": ["items"],
                "additionalProperties": False,
            },
            handler=lambda args: repository.update(_dict_list(args.get("items"))),
            approval_preview=lambda args: _write_preview("更新", _dict_list(args.get("items"))),
        ),
        create_executable_tool(
            name="thingsImport",
            need_confirm=True,
            description="从外部来源导入事项到 Telos Things 本地库。当前支持 provider=apple。",
            parameters={
                "type": "object",
                "properties": {
                    "provider": {"type": "string", "enum": ["apple"], "description": "外部来源。"},
                    "filters": {
                        "type": "object",
                        "additionalProperties": True,
                        "description": "传给外部来源的筛选条件。",
                    },
                },
                "required": ["provider"],
                "additionalProperties": False,
            },
            handler=lambda args: _things_import(repository, args),
            approval_preview=lambda args: f"Things 从 {args.get('provider') or '外部来源'} 导入",
        ),
        create_executable_tool(
            name="thingsExport",
            need_confirm=True,
            description="把 Telos Things 本地库中的 task/event 导出到外部来源。当前支持 provider=apple。",
            parameters={
                "type": "object",
                "properties": {
                    "provider": {"type": "string", "enum": ["apple"], "description": "外部目标。"},
                    "ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                        "description": "要导出的 task/event id。",
                    },
                },
                "required": ["provider", "ids"],
                "additionalProperties": False,
            },
            handler=lambda args: _things_export(repository, args),
            approval_preview=lambda args: f"Things 导出 {len(list(args.get('ids') or []))} 项到 {args.get('provider')}",
        ),
    ]


def _list_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "scheduled_in": _window_schema("按 task.scheduled_at / event.start_at 查询；未传时间窗时默认今天到未来七天。"),
            "due_in": _window_schema("按 deadline 查询。"),
            "completed_in": _window_schema("按 completed_at 查询。"),
            "include_overdue": {"type": "boolean", "description": "是否包含已过期的 open 项。"},
            "include_anytime": {"type": "boolean", "description": "是否额外并入可随时执行的 open task/event。"},
            "include_deferred": {
                "type": "boolean",
                "description": "是否额外并入时间窗内会激活，或时间窗内到期的 someday/suspended 项。",
            },
            "status": {
                "type": "string",
                "enum": ["open", "completed", "archived", "all"],
                "description": "状态筛选；默认 open。completed 只会命中 task。",
            },
            "schedule": {
                "oneOf": [
                    {"type": "string", "enum": ["someday", "suspended", "anytime", "scheduled"]},
                    {
                        "type": "array",
                        "items": {"type": "string", "enum": ["someday", "suspended", "anytime", "scheduled"]},
                    },
                ],
                "description": "日程状态筛选；数组内为或关系。",
            },
            "type": {
                "oneOf": [
                    {"type": "string", "enum": THING_TYPES},
                    {"type": "array", "items": {"type": "string", "enum": THING_TYPES}},
                ],
                "description": "对象类型；默认 task+event。",
            },
            "domain": _nullable_ref_schema("所属领域；null 表示无领域。"),
            "list": _nullable_ref_schema("所属清单；null 表示无清单。"),
            "section": _nullable_ref_schema("所属分节；null 表示无分节。"),
            "tags": {
                "type": "object",
                "properties": {
                    "all": {"type": "array", "items": {"type": "string"}},
                    "any": {"type": "array", "items": {"type": "string"}},
                    "none": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": False,
                "description": "标签筛选。",
            },
            "text": {"type": "string", "description": "按标题搜索。"},
            "sort": {"type": "string", "enum": ["when", "deadline", "created"], "description": "排序方式。"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "description": "返回数量上限，默认 100。"},
            "cursor": {"type": "string", "description": "分页游标；当前版本固定返回 null。"},
        },
        "additionalProperties": False,
    }


def _create_item_schema() -> dict[str, Any]:
    return {
        "oneOf": [
            _typed_schema("domain", ["title", "description", "tags"]),
            _typed_schema("list", ["domain", "title", "description", "tags"]),
            _typed_schema("section", ["domain", "list", "title", "description"], required=["type", "list", "title"]),
            _typed_schema(
                "task",
                [
                    "domain",
                    "list",
                    "section",
                    "title",
                    "description",
                    "location",
                    "attachments",
                    "tags",
                    "parent_task",
                    "blocked_by",
                    "defer_until",
                    "deadline",
                    "scheduled_at",
                    "fixed",
                    "reminder",
                    "recurrence",
                ],
            ),
            _typed_schema(
                "event",
                [
                    "domain",
                    "list",
                    "section",
                    "title",
                    "description",
                    "location",
                    "attachments",
                    "tags",
                    "defer_until",
                    "start_at",
                    "duration",
                    "fixed",
                    "deadline",
                    "reminder",
                    "recurrence",
                ],
            ),
        ],
        "description": "按 type 区分创建字段。",
    }


def _update_item_schema() -> dict[str, Any]:
    properties = _editable_properties(nullable=True)
    properties.update(
        {
            "target": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "type": {"type": "string", "enum": THING_TYPES},
                    "title": {"type": "string", "minLength": 1},
                    "occurrence_date": {"type": "string"},
                },
                "additionalProperties": False,
                "description": "要更新的对象；优先用 id。",
            },
            "type": {"type": "string", "enum": ITEM_TYPES, "description": "仅支持 task/event 互转。"},
            "status": {
                "type": "string",
                "enum": ["open", "completed", "archived"],
                "description": "目标状态；completed 仅适用于 task，event 只能设为 open 或 archived。",
            },
        }
    )
    return {
        "type": "object",
        "properties": properties,
        "required": ["target"],
        "additionalProperties": False,
    }


def _detail_target_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": THING_TYPES},
            "item": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "title": {"type": "string", "minLength": 1},
                },
                "additionalProperties": False,
            },
            "id": {"type": "string", "minLength": 1},
        },
        "additionalProperties": False,
    }


def _typed_schema(
    item_type: str,
    field_names: list[str],
    *,
    required: list[str] | None = None,
) -> dict[str, Any]:
    all_properties = _editable_properties(nullable=False)
    properties = {
        "type": {"type": "string", "enum": [item_type], "description": "对象类型。"},
        **{name: all_properties[name] for name in field_names},
    }
    return {
        "type": "object",
        "properties": properties,
        "required": required or ["type", "title"],
        "additionalProperties": False,
    }


def _editable_properties(*, nullable: bool) -> dict[str, Any]:
    string_type = ["string", "null"] if nullable else "string"
    bool_type = ["boolean", "null"] if nullable else "boolean"
    array_type = ["array", "null"] if nullable else "array"
    object_type = ["object", "null"] if nullable else "object"
    return {
        "domain": _nullable_ref_schema("所属领域。") if nullable else _ref_schema("所属领域。"),
        "list": _nullable_ref_schema("所属清单。") if nullable else _ref_schema("所属清单。"),
        "section": _nullable_ref_schema("所属分节。") if nullable else _ref_schema("所属分节。"),
        "title": {"type": "string", "minLength": 1, "description": "标题。"},
        "description": {"type": string_type, "description": "描述。"},
        "location": {"type": string_type, "description": "地点。"},
        "attachments": {"type": array_type, "items": {"type": "string"}, "description": "附件路径或 URL。"},
        "tags": {"type": array_type, "items": {"type": "string"}, "description": "标签数组。"},
        "parent_task": _nullable_ref_schema("父任务。") if nullable else _ref_schema("父任务。"),
        "blocked_by": {
            "type": array_type,
            "items": _ref_schema("依赖任务。"),
            "description": "阻塞当前任务的任务列表。",
        },
        "defer_until": {
            "type": string_type,
            "description": "someday、YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS；空表示不推迟。",
        },
        "deadline": {"type": string_type, "description": "截止时间；YYYY-MM-DD 会归一到当天 23:59:59。"},
        "scheduled_at": {"type": string_type, "description": "task 的计划时间；YYYY-MM-DD 表示只指定日期。"},
        "start_at": {"type": string_type, "description": "event 的开始时间。"},
        "duration": {
            "type": object_type,
            "properties": {
                "minutes": {"type": "integer", "minimum": 0},
                "days": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": False,
            "description": "event 持续时间。",
        },
        "fixed": {"type": bool_type, "description": "时间是否固定。"},
        "reminder": {
            "type": array_type,
            "items": _reminder_schema(),
            "description": "提醒数组；整体替换。",
        },
        "recurrence": {
            **_recurrence_schema(),
            "type": object_type,
            "description": "重复规则；null 或空对象表示清空。",
        },
    }


def _ref_schema(description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1},
            "title": {"type": "string", "minLength": 1},
        },
        "additionalProperties": False,
        "description": description,
    }


def _nullable_ref_schema(description: str) -> dict[str, Any]:
    return {
        "oneOf": [
            _ref_schema(description),
            {"type": "null"},
        ],
        "description": description,
    }


def _window_schema(description: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "from": {"type": "string"},
            "to": {"type": "string"},
        },
        "additionalProperties": False,
        "description": description,
    }


def _reminder_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "before_minutes": {"type": "integer", "minimum": 0},
            "at": {"type": "string"},
        },
        "anyOf": [
            {"type": "object", "required": ["before_minutes"]},
            {"type": "object", "required": ["at"]},
        ],
        "additionalProperties": False,
    }


def _recurrence_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "frequency": {"type": "string", "enum": ["day", "week", "month", "year"]},
            "on_non_workday": {"type": "string", "enum": ["skip", "before", "after"]},
            "interval": {"type": "integer", "minimum": 1},
            "days_of_week": {"type": "array", "items": {"type": "string", "enum": WEEKDAYS}},
            "weekdays_of_month": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "week_number": {"type": "integer"},
                        "weekdays": {"type": "array", "items": {"type": "string", "enum": WEEKDAYS}},
                    },
                    "required": ["week_number", "weekdays"],
                    "additionalProperties": False,
                },
            },
            "days_of_month": {"type": "array", "items": {"type": "integer", "minimum": -31, "maximum": 31}},
            "months_of_year": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 12}},
            "anchor": {"type": "string", "enum": ["schedule", "completion"]},
            "ends": {
                "type": "object",
                "properties": {
                    "after_count": {"type": "integer", "minimum": 1},
                    "on_time": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    }


def _dict_list(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in list(value or []) if isinstance(item, dict)]


def _string_list(value: Any) -> list[str]:
    return [text for text in (str(item or "").strip() for item in list(value or [])) if text]


def _things_import(repository: ThingsRepository, args: dict[str, Any]) -> dict[str, Any]:
    provider = str(args.get("provider") or "").strip()
    if provider != "apple":
        return {"imported": [], "failed": [{"error": f"不支持的 provider: {provider}"}]}
    return import_apple_items(
        repository,
        AppleNativeJsonHelperClient(),
        filters=dict(args.get("filters") or {}),
    )


def _things_export(repository: ThingsRepository, args: dict[str, Any]) -> dict[str, Any]:
    provider = str(args.get("provider") or "").strip()
    if provider != "apple":
        return {"exported": [], "failed": [{"error": f"不支持的 provider: {provider}"}]}
    return export_apple_items(
        repository,
        AppleNativeJsonHelperClient(),
        ids=_string_list(args.get("ids")),
    )


def _write_preview(action: str, items: list[dict[str, Any]]) -> str:
    labels = [
        str(item.get("title") or item.get("target", {}).get("id") or item.get("type") or "").strip()
        for item in items
    ]
    labels = [label for label in labels if label]
    suffix = f"：{', '.join(labels[:5])}" if labels else ""
    return f"Things {action} {len(items)} 项{suffix}"
