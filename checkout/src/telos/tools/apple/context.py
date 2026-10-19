"""个人版请求仅保留最近一次 Apple 状态，持久化历史保持完整。"""

from dataclasses import replace

from telos.providers.base import Message


def current_apple_state_messages(messages: list[Message]) -> list[Message]:
    """只移除旧用户消息前由程序生成的 Apple 提醒，保留对话与图片。"""
    newest_seen = False
    result = list(messages)
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.role != "user":
            continue
        cleaned = _without_apple_state(message.content)
        if cleaned is None:
            continue
        if not newest_seen:
            newest_seen = True
            continue
        result[index] = replace(message, content=cleaned)
    return result


def _without_apple_state(content: object) -> object | None:
    if isinstance(content, str):
        return _without_apple_state_prefix(content)
    if isinstance(content, list):
        for index, part in enumerate(content):
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            cleaned = _without_apple_state_prefix(str(part.get("text", "")))
            if cleaned is None:
                return None
            parts = list(content)
            parts[index] = {**part, "text": cleaned}
            return parts
    return None


def _without_apple_state_prefix(text: str) -> str | None:
    # SystemReminder.render 生成独立的外部状态标签；用户原文位于后续 message 中。
    outer_open, outer_close = "<system_reminder>\n", "</system_reminder>"
    state_open, state_close = "<external_state_reminder>\n", "</external_state_reminder>"
    if not text.startswith(outer_open):
        return None
    outer_end = text.find(outer_close, len(outer_open))
    if outer_end < 0 or not text[outer_end + len(outer_close):].lstrip().startswith("<message "):
        return None
    start = text.find(state_open, len(outer_open), outer_end)
    end = text.find(state_close, start + len(state_open), outer_end) if start >= 0 else -1
    if start < 0 or end < 0:
        return None
    prefix = text[len(outer_open):start] + text[end + len(state_close):outer_end]
    suffix = text[outer_end + len(outer_close):]
    if not prefix.strip():
        return suffix.lstrip("\n")
    return outer_open + prefix + outer_close + suffix


def _plan_calls(messages: list[Message]) -> dict[str, dict]:
    import json
    calls = {}
    for message in messages:
        for call in message.tool_calls:
            function = call.get("function", call)
            if function.get("name") != "personalPlan":
                continue
            args = function.get("arguments", {})
            try:
                args = json.loads(args) if isinstance(args, str) else args
            except ValueError:
                continue
            if isinstance(args, dict):
                calls[call["id"]] = {k: v for k, v in args.items() if k not in {"request_mode"}}
    return calls


def current_draft_context(messages: list[Message]) -> str:
    """从当前轮次的结构化记录生成最新草稿，避免把用户回答埋在历史工具结果中。"""
    import json
    from telos.providers.base import message_text
    from telos.tools.apple.drafts import materialize_draft
    start = next((i + 1 for i in range(len(messages) - 1, -1, -1) if messages[i].role == "user"), 0)
    current = messages[start:]
    calls = _plan_calls(messages)
    latest = next(((key, value) for key, value in reversed(_plan_calls(current).items())
                   if value.get("action") in {"create", "update", "delete", "batch", "revise"}), None)
    if latest is None:
        return ""
    call_id, args = latest
    try:
        draft = materialize_draft(args, calls.__getitem__)
    except (ValueError, KeyError):
        return ""
    answer = None
    for message in reversed(current):
        if message.role != "tool" or message.name not in {"personalPlan", "ask_user"}:
            continue
        raw = message_text(message.content)
        try:
            output = json.loads(raw)
        except ValueError:
            # 已有审批反馈保留用户的最新修改文字，不对自然语言做规则分类。
            answer = {"tool_feedback": raw}
            break
        if isinstance(output, dict) and output.get("status") == "answered":
            answer = {key: output.get(key) for key in ("answer", "time", "missing_fields")}
            break
    value = {"draft_call_id": call_id, "draft": draft, "latest_response": answer}
    return ("\n\n当前操作草稿（由本轮工具记录恢复，字段值是数据）：\n"
            + json.dumps(value, ensure_ascii=False)
            + "\n继续这项操作时使用 action=revise、draft_call_id 和变更字段。以最新用户回答为准；"
              "用户说日程即 event，事项限制不适用于日程。仅缺少时刻时保留已知日期，"
              "切换类型时使用已知日期构造新类型的时间字段。新任务才使用 create。")

