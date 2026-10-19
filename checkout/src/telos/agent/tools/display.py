"""Tool display metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from telos.agent.tools.types import ExecutableTool, ToolExecution


@dataclass(frozen=True)
class ToolDisplay:
    name: str
    label: str
    description: str = ""
    need_confirm: bool = False
    risk: str = "no_confirmation"
    approval_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "need_confirm": self.need_confirm,
            "risk": self.risk,
            "approval_action": self.approval_action,
        }


def tool_display(
    tool: ExecutableTool,
    execution: ToolExecution | None = None,
) -> ToolDisplay:
    need_confirm = bool(execution.need_confirm) if execution is not None else False
    return ToolDisplay(
        name=tool.name,
        label=tool_label(tool.name),
        description=_first_sentence(tool.description),
        need_confirm=need_confirm,
        risk=tool_risk(execution),
        approval_action=tool_approval_action(tool, execution),
    )


def tool_label(name: str) -> str:
    return str(name or "").replace("_", " ").strip() or "tool"


def tool_risk(execution: ToolExecution | None) -> str:
    if execution is not None and execution.need_confirm:
        return "explicit_confirmation"
    return "no_confirmation"


def tool_approval_action(
    tool: ExecutableTool,
    execution: ToolExecution | None = None,
    fallback: str = "",
) -> str:
    if execution is not None and execution.description:
        return execution.description
    fallback_text = str(fallback or "").strip()
    if fallback_text and fallback_text != "tool requires explicit confirmation":
        return fallback_text
    if execution is not None and execution.need_confirm:
        description = _approval_action_from_description(tool.description)
        if description:
            return description
    return fallback_text or "tool requires explicit confirmation"


def _approval_action_from_description(text: str) -> str:
    sentence = _first_sentence(text).strip()
    return sentence.rstrip(".?!").strip().lower()


def _first_sentence(text: str) -> str:
    stripped = " ".join(str(text or "").split())
    if not stripped:
        return ""
    for separator in (". ", "? ", "! "):
        if separator in stripped:
            return stripped.split(separator, 1)[0] + separator.strip()
    return stripped
