"""Product-facing settings page projection and updates."""

from __future__ import annotations

import hashlib
import plistlib
from xml.parsers.expat import ExpatError
import re
from pathlib import Path
from typing import Any, Mapping

from telos.core.errors import ConfigurationError
from telos.core.settings import TelosSettings, load_config_mapping, write_config_mapping
from telos.providers.extra_body import (
    RESERVED_EXTRA_BODY_KEYS as _RESERVED_EXTRA_BODY_KEYS,
)

_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def settings_revision(path: Path) -> str:
    content = path.read_bytes() if path.exists() else b""
    return hashlib.sha256(content).hexdigest()


def settings_ui_payload(settings: TelosSettings) -> dict[str, Any]:
    raw = dict(settings.agent_config or {})
    providers_raw = _mapping(raw.get("providers"))
    models: list[dict[str, Any]] = []
    for model_id, value in providers_raw.items():
        if model_id == "default" or not isinstance(value, dict):
            continue
        item = dict(value)
        api_key = str(item.get("api_key") or "").strip()
        capabilities = _mapping(item.get("capabilities"))
        models.append(
            {
                "id": str(model_id),
                "name": str(item.get("label") or model_id),
                "type": str(item.get("type") or model_id),
                "model": str(item.get("model") or ""),
                "base_url": str(item.get("base_url") or ""),
                "max_tokens": _optional_number(item.get("max_tokens")),
                "api_key_configured": bool(api_key),
                "capabilities": {
                    "image_in": bool(capabilities.get("image_in")),
                },
                "extra_body": _mapping(item.get("extra_body")),
            }
        )
    tools = []
    for name, entry in settings.tools.entries.items():
        options = dict(entry.options or {})
        tools.append(
            {
                "name": name,
                "description": entry.description,
                "enabled": entry.enabled,
                "approval": entry.approval,
                "options": {
                    "allow": _string_list(options.get("allow")),
                    "deny": _string_list(options.get("deny")),
                    "cwd": str(options.get("cwd") or ""),
                    "timeout_default": _optional_number(options.get("timeout_default")),
                    "timeout_max": _optional_number(options.get("timeout_max")),
                    "max_output_bytes": _optional_number(options.get("max_output_bytes")),
                },
            }
        )
    return {
        "revision": settings_revision(settings.paths.agent_config_path),
        "config_path": str(settings.paths.agent_config_path),
        "models": models,
        "agent": {"model": settings.providers.default},
        "recap": {
            "enabled": settings.memory.recap.enabled,
            "idle_seconds": settings.memory.recap.idle_seconds,
            "min_messages": settings.memory.recap.min_messages,
        },
        "timeline": {
            "enabled": settings.daemon.timeline.enabled,
            "summary_enabled": settings.daemon.timeline.summary.enabled,
            "capture_interval_seconds": settings.daemon.timeline.collector.min_capture_interval_seconds,
            "summary_check_interval_seconds": settings.daemon.timeline.summary.check_interval_seconds,
            "model": settings.daemon.timeline.summary.provider,
            "excluded_apps": list(settings.daemon.timeline.collector.exclude_bundle_prefixes),
        },
        "activity": {
            "enabled": settings.daemon.activity.enabled,
        },
        "tools": {
            "policy_mode": settings.tools.policy.mode,
            "entries": tools,
        },
        "capabilities": {
            "skills": settings.skills.enabled,
            "mcp": settings.mcp.enabled,
            "subagents": sorted(str(name) for name in _mapping(raw.get("subagents"))),
        },
    }


def settings_restart_required(
    active: TelosSettings,
    configured: TelosSettings,
) -> bool:
    """配置文件中尚未应用的设置是否必须通过重启加载。"""

    return _restart_relevant_config(active) != _restart_relevant_config(configured)


def _restart_relevant_config(settings: TelosSettings) -> dict[str, Any]:
    payload = settings_ui_payload(settings)
    models: list[dict[str, Any]] = []
    for value in payload["models"]:
        model = dict(value)
        model.pop("name", None)
        model.pop("api_key_configured", None)
        entry = settings.providers.entries.get(str(model.get("id") or ""))
        model["api_key"] = str(entry.options.get("api_key") or "") if entry else ""
        models.append(model)
    models.sort(key=lambda item: str(item.get("id") or ""))

    tool_entries: list[dict[str, Any]] = []
    for value in payload["tools"]["entries"]:
        entry = dict(value)
        entry.pop("approval", None)
        tool_entries.append(entry)
    tool_entries.sort(key=lambda item: str(item.get("name") or ""))

    return {
        "models": models,
        "agent": payload["agent"],
        "recap": payload["recap"],
        "timeline": payload["timeline"],
        "activity": payload["activity"],
        "tools": tool_entries,
        "capabilities": {
            "skills": payload["capabilities"]["skills"],
            "mcp": payload["capabilities"]["mcp"],
        },
    }


def local_apps_payload() -> list[dict[str, str]]:
    roots = [
        Path("/Applications"),
        Path("/System/Applications"),
        Path("/System/Applications/Utilities"),
        Path.home() / "Applications",
    ]
    apps: dict[str, dict[str, str]] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.glob("*.app")):
            info_path = path / "Contents" / "Info.plist"
            try:
                with info_path.open("rb") as handle:
                    info = plistlib.load(handle)
            except (OSError, ValueError, TypeError, ExpatError):
                continue
            if not isinstance(info, dict):
                continue
            bundle_id = str(info.get("CFBundleIdentifier") or "").strip()
            if not bundle_id:
                continue
            name = str(
                info.get("CFBundleDisplayName")
                or info.get("CFBundleName")
                or path.stem
            ).strip()
            apps[bundle_id] = {
                "bundle_id": bundle_id,
                "name": name or path.stem,
                "path": str(path),
            }
    return sorted(apps.values(), key=lambda item: item["name"].casefold())


def update_settings_from_ui(
    settings: TelosSettings,
    payload: Mapping[str, Any],
) -> None:
    path = settings.paths.agent_config_path
    expected_revision = str(payload.get("revision") or "").strip()
    if not expected_revision or expected_revision != settings_revision(path):
        raise ConfigurationError("配置文件已被其他程序修改，请刷新设置页后重试")
    raw = load_config_mapping(path)
    _update_models(raw, payload)
    _update_recap(raw, payload.get("recap"))
    _update_timeline(raw, payload.get("timeline"))
    _update_activity(raw, payload.get("activity"))
    _update_tools(raw, payload.get("tools"))
    _update_capabilities(raw, payload.get("capabilities"))
    write_config_mapping(path, raw)


def set_tool_approval(settings: TelosSettings, tool_name: str, required: bool) -> None:
    name = str(tool_name or "").strip()
    if not name or name not in settings.tools.entries:
        raise ConfigurationError(f"未知工具：{name or '-'}")
    raw = load_config_mapping(settings.paths.agent_config_path)
    tools = _mapping(raw.get("tools"))
    entries = _mapping(tools.get("entries"))
    entry = _mapping(entries.get(name))
    entry["approval"] = bool(required)
    entries[name] = entry
    tools["entries"] = entries
    raw["tools"] = tools
    write_config_mapping(settings.paths.agent_config_path, raw)


def _update_models(raw: dict[str, Any], payload: Mapping[str, Any]) -> None:
    models_raw = payload.get("models")
    if not isinstance(models_raw, list) or not models_raw:
        raise ConfigurationError("至少需要一个模型配置")
    old_providers = _mapping(raw.get("providers"))
    providers: dict[str, Any] = {}
    ids: set[str] = set()
    for item_raw in models_raw:
        item = _mapping(item_raw)
        model_id = str(item.get("id") or "").strip()
        if not _MODEL_ID.fullmatch(model_id):
            raise ConfigurationError(f"无效的模型配置名称：{model_id or '-'}")
        if model_id in ids:
            raise ConfigurationError(f"模型配置名称重复：{model_id}")
        ids.add(model_id)
        old = _mapping(old_providers.get(model_id))
        entry = dict(old)
        entry["label"] = str(item.get("name") or model_id).strip() or model_id
        entry["type"] = str(item.get("type") or "").strip()
        entry["model"] = str(item.get("model") or "").strip()
        base_url = str(item.get("base_url") or "").strip()
        if base_url:
            entry["base_url"] = base_url
        else:
            entry.pop("base_url", None)
        if "max_tokens" in item:
            if item.get("max_tokens") in (None, ""):
                entry.pop("max_tokens", None)
            else:
                entry["max_tokens"] = _positive_int(
                    item.get("max_tokens"), f"模型配置 {model_id} 的最大输出令牌数"
                )
        if "api_key" in item:
            api_key = str(item.get("api_key") or "").strip()
            if api_key:
                entry["api_key"] = api_key
        if bool(item.get("clear_api_key")):
            entry.pop("api_key", None)
        if "capabilities" in item:
            submitted_capabilities = item.get("capabilities")
            if not isinstance(submitted_capabilities, Mapping):
                raise ConfigurationError(f"模型配置 {model_id} 的能力配置必须是对象")
            capabilities = _mapping(entry.get("capabilities"))
            capabilities["image_in"] = bool(submitted_capabilities.get("image_in"))
            entry["capabilities"] = capabilities
        if "extra_body" in item:
            submitted_extra_body = item.get("extra_body")
            if not isinstance(submitted_extra_body, Mapping):
                raise ConfigurationError(f"模型配置 {model_id} 的额外请求参数必须是对象")
            extra_body: dict[str, Any] = {}
            for raw_key, value in submitted_extra_body.items():
                key = str(raw_key or "").strip()
                if not key:
                    raise ConfigurationError(f"模型配置 {model_id} 的额外请求参数名称不能为空")
                if key in _RESERVED_EXTRA_BODY_KEYS:
                    raise ConfigurationError(
                        f"模型配置 {model_id} 的额外请求参数不能覆盖 {key}"
                    )
                extra_body[key] = value
            if extra_body:
                entry["extra_body"] = extra_body
            else:
                entry.pop("extra_body", None)
        if not entry["type"] or not entry["model"]:
            raise ConfigurationError(f"模型配置 {model_id} 缺少类型或模型名称")
        providers[model_id] = entry
    agent = _mapping(payload.get("agent"))
    default_model = str(agent.get("model") or "").strip()
    timeline = _mapping(payload.get("timeline"))
    timeline_model = str(timeline.get("model") or "").strip()
    if default_model not in ids:
        raise ConfigurationError("主智能体选择的模型配置不存在")
    if timeline_model and timeline_model not in ids:
        raise ConfigurationError("屏幕活动选择的模型配置不存在")
    raw["providers"] = {"default": default_model, **providers}


def _update_recap(raw: dict[str, Any], value: Any) -> None:
    item = _mapping(value)
    memory = _mapping(raw.get("memory"))
    recap = _mapping(memory.get("recap"))
    recap["enabled"] = bool(item.get("enabled"))
    recap["idle_seconds"] = _non_negative_int(item.get("idle_seconds"), "Recap 空闲时间")
    recap["min_messages"] = _non_negative_int(item.get("min_messages"), "Recap 最少消息数")
    memory["recap"] = recap
    raw["memory"] = memory


def _update_timeline(raw: dict[str, Any], value: Any) -> None:
    item = _mapping(value)
    daemon = _mapping(raw.get("daemon"))
    timeline = _mapping(daemon.get("timeline"))
    collector = _mapping(timeline.get("collector"))
    summary = _mapping(timeline.get("summary"))
    timeline["enabled"] = bool(item.get("enabled"))
    summary["enabled"] = bool(item.get("summary_enabled"))
    collector["min_capture_interval_seconds"] = _positive_float(
        item.get("capture_interval_seconds"), "屏幕活动最短记录间隔"
    )
    summary["check_interval_seconds"] = _positive_int(
        item.get("summary_check_interval_seconds"), "屏幕活动总结检查间隔"
    )
    summary["provider"] = str(item.get("model") or "").strip()
    collector["exclude_bundle_prefixes"] = _string_list(item.get("excluded_apps"))
    timeline["collector"] = collector
    timeline["summary"] = summary
    daemon["timeline"] = timeline
    raw["daemon"] = daemon


def _update_activity(raw: dict[str, Any], value: Any) -> None:
    """只写 `daemon.activity.enabled`。

    两条环路各自的 provider、间隔和门槛仍然只在配置文件中设置，设置页不覆写它们。
    """
    item = _mapping(value)
    daemon = _mapping(raw.get("daemon"))
    activity = _mapping(daemon.get("activity"))
    activity["enabled"] = bool(item.get("enabled"))
    daemon["activity"] = activity
    raw["daemon"] = daemon


def _update_tools(raw: dict[str, Any], value: Any) -> None:
    item = _mapping(value)
    mode = str(item.get("policy_mode") or "").strip().lower()
    if mode not in {"always_ask", "custom", "yolo"}:
        raise ConfigurationError("无效的工具审批模式")
    submitted = item.get("entries")
    if not isinstance(submitted, list):
        raise ConfigurationError("工具配置格式无效")
    tools = _mapping(raw.get("tools"))
    old_entries = _mapping(tools.get("entries"))
    seen: set[str] = set()
    for submitted_raw in submitted:
        tool = _mapping(submitted_raw)
        name = str(tool.get("name") or "").strip()
        if not name or name not in old_entries or name in seen:
            raise ConfigurationError(f"未知或重复的工具配置：{name or '-'}")
        seen.add(name)
        entry = _mapping(old_entries[name])
        entry["enabled"] = bool(tool.get("enabled"))
        approval = tool.get("approval", entry.get("approval"))
        if approval is not None and not isinstance(approval, bool):
            raise ConfigurationError(f"无效的工具审批配置：{name}")
        entry["approval"] = approval
        incoming_options = _mapping(tool.get("options"))
        options = _mapping(entry.get("options"))
        for key in ("allow", "deny"):
            if key in incoming_options:
                options[key] = _string_list(incoming_options.get(key))
        for key in ("cwd", "timeout_default", "timeout_max", "max_output_bytes"):
            if key in incoming_options and incoming_options.get(key) not in (None, ""):
                options[key] = incoming_options[key]
        if options:
            entry["options"] = options
        old_entries[name] = entry
    tools["policy"] = {"mode": mode}
    tools["entries"] = old_entries
    raw["tools"] = tools


def _update_capabilities(raw: dict[str, Any], value: Any) -> None:
    item = _mapping(value)
    for key in ("skills", "mcp"):
        section = _mapping(raw.get(key))
        section["enabled"] = bool(item.get(key))
        raw[key] = section


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _string_list(value: Any) -> list[str]:
    return [str(item).strip() for item in value] if isinstance(value, list) else []


def _optional_number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _non_negative_int(value: Any, label: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"{label}必须是整数") from error
    if result < 0:
        raise ConfigurationError(f"{label}不能小于 0")
    return result


def _positive_int(value: Any, label: str) -> int:
    result = _non_negative_int(value, label)
    if result <= 0:
        raise ConfigurationError(f"{label}必须大于 0")
    return result


def _positive_float(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"{label}必须是数字") from error
    if result <= 0:
        raise ConfigurationError(f"{label}必须大于 0")
    return result
