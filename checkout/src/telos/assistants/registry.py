"""长期助手注册表：内置助手与 agent config ``assistants:`` 节的自建助手。

长期助手是有独立持续会话、独立系统提示和工具集、可被主 agent 通过 ``assistant``
工具委派的长期角色。内置助手只有 ``things_coach``，profile 是随包分发的
``profiles/things_coach.yaml``，位置按 ``default_source_root()`` 解析；自建助手在
agent config YAML 的 ``assistants:`` 节声明，条目结构沿用 profile YAML 形态，
``system_prompt.prompt_template`` 相对 config 文件所在目录解析。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telos.agent.context.prompts import AgentProfile, load_profile, profile_from_mapping
from telos.core.errors import ConfigurationError
from telos.storage.paths import default_source_root

THINGS_COACH_ASSISTANT_ID = "things_coach"
THINGS_COACH_LABEL = "Things 助手"

_ASSISTANT_ID_PATTERN = re.compile(r"[a-z0-9_]+")
# 「main」与主 surface 冲突，不能作为助手 id。
_RESERVED_IDS = {"main"}


def _builtin_profile_path() -> Path:
    """内置助手 profile 的位置，与进程 cwd 无关。

    ``profiles/`` 是随包分发的资源，只能用 :func:`default_source_root` 定位。按
    cwd 推断的 repo_root 在打包后的 app 里是 ``/``，读不到该文件。
    """
    return default_source_root() / "profiles" / f"{THINGS_COACH_ASSISTANT_ID}.yaml"


@dataclass(frozen=True)
class AssistantDefinition:
    """一个长期助手的静态定义。"""

    id: str
    label: str
    description: str = ""
    builtin: bool = False
    # 内置助手走仓库 profile 文件；自建助手走 agent config 里的 mapping。
    profile_path: Path | None = None
    profile_config: dict[str, Any] = field(default_factory=dict)
    profile_base_dir: Path | None = None


class AssistantRegistry:
    """解析并校验全部可用助手，按 id 提供查询。"""

    def __init__(
        self,
        *,
        assistants: dict[str, Any] | None = None,
        config_base_dir: Path | None = None,
    ):
        self.config_base_dir = Path(config_base_dir) if config_base_dir is not None else None
        self._definitions: dict[str, AssistantDefinition] = {
            THINGS_COACH_ASSISTANT_ID: AssistantDefinition(
                id=THINGS_COACH_ASSISTANT_ID,
                label=THINGS_COACH_LABEL,
                description=self._builtin_description(),
                builtin=True,
                profile_path=_builtin_profile_path(),
            )
        }
        for assistant_id, entry in self._parse_custom(assistants or {}).items():
            self._definitions[assistant_id] = entry

    @classmethod
    def from_settings(cls, settings: Any) -> AssistantRegistry:
        return cls(
            assistants=getattr(settings, "assistants", None)
            or (settings.agent_config or {}).get("assistants"),
            config_base_dir=settings.paths.config_base_dir,
        )

    def ids(self) -> list[str]:
        return sorted(self._definitions)

    def list(self) -> list[AssistantDefinition]:
        return [self._definitions[key] for key in self.ids()]

    def get(self, assistant_id: str) -> AssistantDefinition:
        definition = self._definitions.get(str(assistant_id or "").strip())
        if definition is None:
            raise ConfigurationError(f"未知助手：{assistant_id or '-'}")
        return definition

    def has(self, assistant_id: str) -> bool:
        return str(assistant_id or "").strip() in self._definitions

    def profile_for(self, definition: AssistantDefinition) -> AgentProfile:
        if definition.builtin:
            return load_profile(definition.profile_path)
        return profile_from_mapping(
            definition.profile_config,
            base_dir=definition.profile_base_dir,
        )

    def _builtin_description(self) -> str:
        return load_profile(_builtin_profile_path()).description

    def _parse_custom(
        self,
        assistants: dict[str, Any],
    ) -> dict[str, AssistantDefinition]:
        if not isinstance(assistants, dict):
            raise ConfigurationError("assistants 节必须是对象")
        definitions: dict[str, AssistantDefinition] = {}
        for raw_id, raw_entry in assistants.items():
            assistant_id = validate_assistant_id(
                raw_id,
                taken=set(self._definitions) | set(definitions),
            )
            entry = raw_entry if isinstance(raw_entry, dict) else {}
            profile_config = dict(entry)
            profile_config.setdefault("name", assistant_id)
            _validate_tool_entries(assistant_id, profile_config)
            definitions[assistant_id] = AssistantDefinition(
                id=assistant_id,
                label=str(entry.get("label") or entry.get("name") or assistant_id).strip()
                or assistant_id,
                description=str(entry.get("description") or "").strip(),
                builtin=False,
                profile_config=profile_config,
                profile_base_dir=self.config_base_dir,
            )
        return definitions


def validate_assistant_id(raw_id: Any, *, taken: set[str] | None = None) -> str:
    """校验自建助手 id：小写字母/数字/下划线，不为保留 id，不与已有助手重复。"""
    assistant_id = str(raw_id or "").strip()
    if not assistant_id or not _ASSISTANT_ID_PATTERN.fullmatch(assistant_id):
        raise ConfigurationError(
            f"无效的助手 id：{assistant_id or '-'}（只能用小写字母、数字和下划线）"
        )
    if assistant_id in _RESERVED_IDS:
        raise ConfigurationError(f"助手 id 不能使用保留名：{assistant_id}")
    if taken and assistant_id in taken:
        raise ConfigurationError(f"助手 id 重复或与内置助手冲突：{assistant_id}")
    return assistant_id


def _validate_tool_entries(assistant_id: str, profile_config: dict[str, Any]) -> None:
    tools = profile_config.get("tools")
    if not isinstance(tools, dict):
        return
    entries = tools.get("entries")
    if not isinstance(entries, dict):
        return
    for name in entries:
        if not str(name or "").strip():
            raise ConfigurationError(f"助手 {assistant_id} 的工具名不能为空")
