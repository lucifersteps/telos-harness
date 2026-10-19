"""长期助手机制：有独立持续会话、系统提示和工具集、可被主 agent 委派的角色。"""

from telos.assistants.registry import (
    THINGS_COACH_ASSISTANT_ID,
    AssistantDefinition,
    AssistantRegistry,
)
from telos.assistants.service import AssistantResult, AssistantService

__all__ = [
    "THINGS_COACH_ASSISTANT_ID",
    "AssistantDefinition",
    "AssistantRegistry",
    "AssistantResult",
    "AssistantService",
]
