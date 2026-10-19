"""执行前错误作为普通工具结果反馈；总步数由既有循环限制。"""
import json


def payload(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def is_input_error(value):
    value = payload(value)
    return bool(value and value.get("status") == "input_error"
                and value.get("execution_started") is False)

