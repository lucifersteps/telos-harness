"""Web/WS 控制台适配层：把 agent 流式 turn 暴露给本地 web/桌面端 UI。"""

from telos.adapters.web.adapter import WebAdapter
from telos.adapters.web.approval_registry import (
    PendingApprovalRegistry,
    RegistryApprovalChannel,
)
from telos.adapters.question_registry import (
    PendingQuestionRegistry,
    RegistryQuestionChannel,
)

__all__ = [
    "PendingApprovalRegistry",
    "PendingQuestionRegistry",
    "RegistryApprovalChannel",
    "RegistryQuestionChannel",
    "WebAdapter",
]
