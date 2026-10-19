"""Lark interactive card helpers."""

from __future__ import annotations

from telos.adapters.base import ApprovalRequest


def build_approval_card(request: ApprovalRequest) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**{request.tool_name}** requires approval: {request.action}",
                },
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Approve"},
                        "type": "primary",
                        "value": {"tool_call_id": request.tool_call_id, "decision": "approve"},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "Cancel"},
                        "type": "default",
                        "value": {"tool_call_id": request.tool_call_id, "decision": "deny"},
                    },
                ],
            },
        ],
    }
