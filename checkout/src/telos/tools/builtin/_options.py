"""Shared tool-option parsing helpers for the built-in file tools."""

from __future__ import annotations

import os
from typing import Any


def option_list(options: dict[str, Any], key: str) -> list[str]:
    value = options.get(key)
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def resolve_env_ref(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 3 and text.startswith("{") and text.endswith("}"):
        return os.getenv(text[1:-1].strip(), "")
    return text
