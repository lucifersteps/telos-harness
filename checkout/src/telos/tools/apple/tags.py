"""Reminder tag fields; no state or separate write path."""

TAG_SCHEMA = {"type": "array", "items": {"type": "string", "minLength": 1},
    "uniqueItems": True,
    "description": "仅提醒事项：完整标签名称列表，不带#、不含空白。替换全部标签；[]清空，省略保留。追加时先读取当前tags并合并，避免丢失已有标签。"}


def validate_tags(kind, fields):
    if "tags" not in fields:
        return
    if kind != "reminder":
        raise ValueError("标签仅适用于提醒事项。")
    tags = fields["tags"]
    if not isinstance(tags, list) or any(not isinstance(t, str) or not t or
            "#" in t or any(c.isspace() for c in t) for t in tags):
        raise ValueError("标签须为不带#、不含空白的非空名称列表；[]用于清空。")
    if len({t.lower() for t in tags}) != len(tags):
        raise ValueError("标签不能重复（忽略大小写）。")
