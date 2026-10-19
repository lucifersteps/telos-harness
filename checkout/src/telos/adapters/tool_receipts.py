"""Adapter-visible tool execution receipts."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase


@dataclass(frozen=True)
class ToolReceipt:
    tool_name: str
    text: str


@dataclass(frozen=True)
class ToolReceiptConfig:
    enabled: bool = False
    default: bool = False
    tools: dict[str, bool] | None = None
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    def should_send(self, tool_name: str) -> bool:
        decision = self.default
        for pattern, enabled in dict(self.tools or {}).items():
            if _matches(pattern, tool_name):
                decision = enabled
        if self.include and any(_matches(pattern, tool_name) for pattern in self.include):
            decision = True
        if self.exclude and any(_matches(pattern, tool_name) for pattern in self.exclude):
            decision = False
        return bool(self.enabled and decision)


def collect_tool_receipts(
    events: list[object],
    options: object | None,
    *,
    adapter_name: str = "",
    platform: str = "",
) -> list[ToolReceipt]:
    config = resolve_tool_receipt_config(
        options,
        adapter_name=adapter_name,
        platform=platform,
    )
    if not config.enabled:
        return []
    receipts: list[ToolReceipt] = []
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "tool.result":
            continue
        tool_name = str(event.get("tool_name") or "").strip()
        if not tool_name or not config.should_send(tool_name):
            continue
        ok = not bool(event.get("is_error"))
        receipts.append(
            ToolReceipt(
                tool_name=tool_name,
                text=f"{tool_name} {'执行成功' if ok else '执行失败'}",
            )
        )
    return receipts


def resolve_tool_receipt_config(
    options: object | None,
    *,
    adapter_name: str = "",
    platform: str = "",
) -> ToolReceiptConfig:
    selected = _select_receipt_options(
        options,
        adapter_name=adapter_name,
        platform=platform,
    )
    return _parse_receipt_config(selected)


def _select_receipt_options(
    options: object | None,
    *,
    adapter_name: str,
    platform: str,
) -> object | None:
    if _looks_like_receipt_options(options):
        return options
    if not isinstance(options, dict):
        return None
    selected: object | None = None
    if "tool_receipts" in options:
        selected = _merge_receipt_options(selected, options.get("tool_receipts"))
    for key in _ordered_non_empty(adapter_name, platform):
        scoped = options.get(key)
        if not isinstance(scoped, dict):
            continue
        if "tool_receipts" in scoped:
            selected = _merge_receipt_options(selected, scoped.get("tool_receipts"))
    return selected


def _parse_receipt_config(value: object | None) -> ToolReceiptConfig:
    if isinstance(value, bool):
        return ToolReceiptConfig(enabled=value, default=value, tools={})
    if not isinstance(value, dict):
        return ToolReceiptConfig()

    raw_tools = value.get("tools")
    tools = _tool_switches(raw_tools)
    include = tuple(str(item).strip() for item in _as_list(value.get("include")) if str(item).strip())
    exclude = tuple(str(item).strip() for item in _as_list(value.get("exclude")) if str(item).strip())
    selectors_present = bool(tools or include or exclude)

    enabled_value = _optional_bool(value.get("enabled"))
    default_value = _optional_bool(value.get("default"))
    enabled = (
        enabled_value
        if enabled_value is not None
        else bool((default_value is True) or selectors_present)
    )
    default = (
        default_value
        if default_value is not None
        else bool(enabled and not selectors_present)
    )
    return ToolReceiptConfig(
        enabled=enabled,
        default=default,
        tools=tools,
        include=include,
        exclude=exclude,
    )


def _merge_receipt_options(left: object | None, right: object | None) -> object | None:
    if right is None:
        return left
    if isinstance(right, bool) or isinstance(left, bool) or not isinstance(right, dict):
        return right
    if not isinstance(left, dict):
        return dict(right)
    merged = dict(left)
    for key, value in right.items():
        if key == "tools" and isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**dict(merged[key]), **value}
            continue
        merged[key] = value
    return merged


def _looks_like_receipt_options(value: object | None) -> bool:
    if isinstance(value, bool):
        return True
    if not isinstance(value, dict):
        return False
    return any(key in value for key in {"enabled", "default", "tools", "include", "exclude"})


def _tool_switches(value: object) -> dict[str, bool]:
    if not isinstance(value, dict):
        return {}
    switches: dict[str, bool] = {}
    for raw_pattern, raw_enabled in value.items():
        pattern = str(raw_pattern or "").strip()
        if not pattern:
            continue
        enabled = _tool_enabled(raw_enabled)
        if enabled is None:
            continue
        switches[pattern] = enabled
    return switches


def _tool_enabled(value: object) -> bool | None:
    if isinstance(value, dict):
        return _optional_bool(value.get("enabled"))
    return _optional_bool(value)


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _as_list(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def _ordered_non_empty(*values: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = str(value or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def _matches(pattern: str, tool_name: str) -> bool:
    pattern = str(pattern or "").strip()
    return pattern == tool_name or fnmatchcase(tool_name, pattern)
