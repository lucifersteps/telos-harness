"""Onboarding 使用的配置文件局部更新工具。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from telos.core.settings import load_config_mapping, write_config_mapping


def update_agent_config_file(
    path: Path,
    *,
    values: dict[str, Any] | None = None,
    remove: list[str] | None = None,
) -> None:
    raw = load_config_mapping(path)
    for key in sorted((remove or []), key=lambda item: item.count("."), reverse=True):
        _delete_path(raw, key)
    for key, value in sorted((values or {}).items(), key=lambda item: item[0].count(".")):
        _set_path(raw, key, value)
    write_config_mapping(path, raw)


def _set_path(data: dict[str, Any], dotted: str, value: Any) -> None:
    parts = [part for part in str(dotted).split(".") if part]
    if not parts:
        return
    current = data
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def _delete_path(data: dict[str, Any], dotted: str) -> None:
    parts = [part for part in str(dotted).split(".") if part]
    if not parts:
        return
    current = data
    parents: list[tuple[dict[str, Any], str]] = []
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            return
        parents.append((current, part))
        current = child
    current.pop(parts[-1], None)
    for parent, key in reversed(parents):
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            parent.pop(key, None)
