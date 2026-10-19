"""Tool manager for built-in, stateful, user, and MCP tools."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from telos.agent.mcp.connection_manager import McpConnectionManager
from telos.agent.skills.registry import SkillRegistry
from telos.agent.subagents.host import SubagentHost
from telos.agent.tools.types import ExecutableTool
from telos.adapters.question_channel import QuestionChannel
from telos.core.errors import ConfigurationError
from telos.core.settings import ToolDefinition, resolve_tool_definition_placeholders
from telos.tools.builtin.ask_user import create_ask_user_tool
from telos.tools.builtin.agent_tool import create_agent_tool
from telos.tools.builtin.bash_tool import create_bash_tool
from telos.tools.builtin.read_tool import create_read_tool
from telos.tools.builtin.weather import create_weather_tool
from telos.tools.builtin.web_search import create_web_search_tool
from telos.tools.builtin.write_edit_tool import create_edit_tool, create_write_tool
from telos.tools.schedule.repository import ScheduleRepository
from telos.tools.schedule.service import ScheduleService
from telos.tools.schedule.tools import create_schedule_tools
from telos.tools.things.repository import ThingsRepository
from telos.tools.things.tools import create_things_tools


class ToolManager:
    def __init__(self):
        self._tools: dict[str, ExecutableTool] = {}

    @classmethod
    def with_builtins(
        cls,
        enabled: Iterable[str],
        *,
        skill_registry: SkillRegistry | None = None,
        subagent_host: SubagentHost | None = None,
        data_dir: Path | None = None,
        memory_space_dir: Path | None = None,
        mcp_manager: McpConnectionManager | None = None,
        repo_root: Path | None = None,
        tool_schema: Iterable[ToolDefinition] | None = None,
        assistant_tool: ExecutableTool | None = None,
        question_channel: QuestionChannel | None = None,
    ) -> "ToolManager":
        manager = cls()
        enabled_items = list(enabled)
        enabled_set = {item for item in enabled_items if not item.startswith("mcp__")}
        definitions = _resolved_tool_schema(
            tool_schema or [],
            repo_root=repo_root,
            data_dir=data_dir,
            memory_space_dir=memory_space_dir,
        )
        _raise_for_removed_tools(enabled_items)
        if definitions:
            manager._register_schema_tools(
                definitions,
                enabled_set,
                subagent_host=subagent_host,
                data_dir=data_dir,
                memory_space_dir=memory_space_dir,
                repo_root=repo_root,
                question_channel=question_channel,
            )
        known = {
            "web_search": create_web_search_tool,
            "get_weather": create_weather_tool,
        }
        for name, factory in known.items():
            if (name in enabled_set or "*" in enabled_set) and name not in manager._tools:
                manager.register(factory())
        if (repo_root is not None or data_dir is not None) and (
            "Read" in enabled_set or "read" in enabled_set or "*" in enabled_set
        ) and "Read" not in manager._tools:
            manager.register(
                create_read_tool(
                    repo_root=repo_root,
                    data_dir=data_dir,
                    memory_space_dir=memory_space_dir,
                )
            )
        if (repo_root is not None or data_dir is not None) and (
            "Bash" in enabled_set or "bash" in enabled_set or "*" in enabled_set
        ) and "Bash" not in manager._tools:
            manager.register(
                create_bash_tool(
                    repo_root=repo_root,
                    data_dir=data_dir,
                    memory_space_dir=memory_space_dir,
                )
            )
        if (repo_root is not None or data_dir is not None) and (
            "Write" in enabled_set or "write" in enabled_set or "*" in enabled_set
        ) and "Write" not in manager._tools:
            manager.register(
                create_write_tool(
                    repo_root=repo_root,
                    data_dir=data_dir,
                    memory_space_dir=memory_space_dir,
                )
            )
        if (repo_root is not None or data_dir is not None) and (
            "Edit" in enabled_set or "edit" in enabled_set or "*" in enabled_set
        ) and "Edit" not in manager._tools:
            manager.register(
                create_edit_tool(
                    repo_root=repo_root,
                    data_dir=data_dir,
                    memory_space_dir=memory_space_dir,
                )
            )
        if (
            subagent_host is not None
            and ("agent" in enabled_set or "*" in enabled_set)
            and "agent" not in manager._tools
        ):
            manager.register(create_agent_tool(subagent_host))
        if (
            assistant_tool is not None
            and ("assistant" in enabled_set or "*" in enabled_set)
            and "assistant" not in manager._tools
        ):
            manager.register(assistant_tool)
        if (
            question_channel is not None
            and ("ask_user" in enabled_set or "*" in enabled_set)
            and "ask_user" not in manager._tools
        ):
            manager.register(create_ask_user_tool(question_channel))
        if data_dir is not None:
            manager._register_stateful_tools(
                enabled_set,
                data_dir,
            )
        if mcp_manager is not None:
            manager.register_mcp_tools(mcp_manager, enabled_items)
        _raise_for_unknown_exact_tools(
            manager,
            enabled_items,
            optional_unavailable=_optional_unavailable_tools(
                subagent_host=subagent_host,
                assistant_available=assistant_tool is not None,
                question_channel_available=question_channel is not None,
            ),
        )
        return manager

    def register(self, tool: ExecutableTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> ExecutableTool | None:
        return self._tools.get(name)

    def list(self) -> list[ExecutableTool]:
        return sorted(self._tools.values(), key=lambda tool: tool.name)

    def apply_schema_overrides(self, schemas: Iterable[dict[str, Any]]) -> None:
        for schema in schemas:
            name = _schema_tool_name(schema)
            tool = self._tools.get(name)
            if tool is None:
                continue
            self._tools[name] = replace(
                tool,
                description=_schema_tool_description(schema) or tool.description,
                parameters=_schema_tool_parameters(schema) or tool.parameters,
            )

    def enabled_for_profile(self, profile_tools: Iterable[str]) -> list[ExecutableTool]:
        patterns = list(profile_tools)
        return [
            tool
            for tool in self.list()
            if any(_matches(pattern, tool.name) for pattern in patterns)
        ]

    def register_mcp_tools(
        self,
        mcp_manager: McpConnectionManager,
        enabled: Iterable[str],
    ) -> None:
        patterns = [item for item in enabled if item.startswith("mcp__") or item == "*"]
        if not patterns:
            return
        for tool in mcp_manager.executable_tools():
            if any(_matches(pattern, tool.name) for pattern in patterns):
                self.register(tool)

    def _register_stateful_tools(
        self,
        enabled_set: set[str],
        data_dir: Path,
    ) -> None:
        tools_dir = data_dir / "tools"
        if _schedule_tools_enabled(enabled_set):
            self._register_matching_tools(
                create_schedule_tools(
                    ScheduleService(ScheduleRepository(tools_dir / "schedule.json")),
                ),
                enabled_set,
            )
        if _things_tools_enabled(enabled_set):
            self._register_matching_tools(
                create_things_tools(ThingsRepository.for_agent_dir(data_dir)),
                enabled_set,
            )

    def _register_matching_tools(
        self,
        tools: Iterable[ExecutableTool],
        enabled_set: set[str],
    ) -> None:
        for tool in tools:
            if any(_matches(pattern, tool.name) for pattern in enabled_set):
                self.register(tool)

    def _register_schema_tools(
        self,
        definitions: list[ToolDefinition],
        enabled_set: set[str],
        *,
        subagent_host: SubagentHost | None,
        data_dir: Path | None,
        memory_space_dir: Path | None,
        repo_root: Path | None,
        question_channel: QuestionChannel | None,
    ) -> None:
        for definition in definitions:
            if not any(_matches(pattern, definition.name) for pattern in enabled_set):
                continue
            tool = _tool_from_backend(
                definition,
                subagent_host=subagent_host,
                data_dir=data_dir,
                memory_space_dir=memory_space_dir,
                repo_root=repo_root,
                question_channel=question_channel,
            )
            self.register(
                _apply_tool_definition(tool, definition),
            )


def _matches(pattern: str, tool_name: str) -> bool:
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        return tool_name.startswith(pattern[:-1])
    return pattern == tool_name


def _schema_tool_name(schema: dict[str, Any]) -> str:
    function = schema.get("function") if isinstance(schema.get("function"), dict) else {}
    return str(function.get("name") or schema.get("name") or "")


def _schema_tool_description(schema: dict[str, Any]) -> str:
    function = schema.get("function") if isinstance(schema.get("function"), dict) else {}
    return str(function.get("description") or schema.get("description") or "")


def _schema_tool_parameters(schema: dict[str, Any]) -> dict[str, Any]:
    function = schema.get("function") if isinstance(schema.get("function"), dict) else {}
    parameters = (
        function.get("parameters")
        or schema.get("parameters")
        or schema.get("input_schema")
    )
    return dict(parameters) if isinstance(parameters, dict) else {}


def _schedule_tools_enabled(enabled_set: set[str]) -> bool:
    if "*" in enabled_set:
        return True
    if "schedule*" in enabled_set:
        return True
    return any(
        item in enabled_set
        for item in ("scheduleCreate", "scheduleList", "scheduleDelete")
    )


def _things_tools_enabled(enabled_set: set[str]) -> bool:
    if "*" in enabled_set:
        return True
    if "things*" in enabled_set:
        return True
    return any(
        item in enabled_set
        for item in (
            "thingsList",
            "thingsDetail",
            "thingsCreate",
            "thingsUpdate",
            "thingsImport",
            "thingsExport",
        )
    )


def _resolved_tool_schema(
    definitions: Iterable[ToolDefinition],
    *,
    repo_root: Path | None,
    data_dir: Path | None,
    memory_space_dir: Path | None,
) -> list[ToolDefinition]:
    by_name: dict[str, ToolDefinition] = {}
    fallback_root = repo_root or data_dir or Path.cwd()
    for definition in definitions:
        by_name[definition.name] = resolve_tool_definition_placeholders(
            definition,
            repo_root=fallback_root,
            data_dir=data_dir,
            memory_space_dir=memory_space_dir,
        )
    return list(by_name.values())


def _raise_for_unknown_exact_tools(
    manager: ToolManager,
    enabled: Iterable[str],
    *,
    optional_unavailable: set[str] | None = None,
) -> None:
    ignored = set(optional_unavailable or set())
    unknown = [
        item
        for item in enabled
        if not _is_pattern(item)
        and not item.startswith("mcp__")
        and item not in ignored
        and item not in manager._tools
    ]
    if unknown:
        raise ConfigurationError(f"Unknown enabled tool(s): {', '.join(unknown)}")


def _optional_unavailable_tools(
    *,
    subagent_host: SubagentHost | None,
    assistant_available: bool = False,
    question_channel_available: bool = False,
) -> set[str]:
    tools = {"skill"}
    if subagent_host is None:
        tools.add("agent")
    if not assistant_available:
        tools.add("assistant")
    if not question_channel_available:
        tools.add("ask_user")
    return tools


def _raise_for_removed_tools(enabled: Iterable[str]) -> None:
    removed: list[str] = []
    for item in enabled:
        text = str(item or "").strip()
        if (
            text == "activitywatch_raw"
            or text.startswith("friend_")
            or text == "friend_*"
            or text.startswith("cron_")
            or text == "cron_*"
        ):
            removed.append(text)
    if removed:
        raise ConfigurationError(f"Removed enabled tool(s): {', '.join(removed)}")


def _is_pattern(value: str) -> bool:
    return str(value or "") == "*" or str(value or "").endswith("*")


def _apply_tool_definition(
    tool: ExecutableTool,
    definition: ToolDefinition,
) -> ExecutableTool:
    return replace(
        tool,
        name=definition.name,
        description=definition.description or tool.description,
        parameters=definition.parameters or tool.parameters,
    )


def _tool_from_backend(
    definition: ToolDefinition,
    *,
    subagent_host: SubagentHost | None,
    data_dir: Path | None,
    memory_space_dir: Path | None,
    repo_root: Path | None,
    question_channel: QuestionChannel | None,
) -> ExecutableTool:
    backend = definition.backend
    options = definition.options
    if backend == "personal.plan":
        from telos.tools.apple.interaction import create_personal_plan_tool

        if data_dir is None:
            raise ConfigurationError("Personal tools require an agent data directory")
        return create_personal_plan_tool(data_dir=data_dir, options=options)
    if backend.startswith("apple."):
        from telos.tools.apple.tools import create_apple_tool

        if data_dir is None:
            raise ConfigurationError("Apple tools require an agent data directory")
        return create_apple_tool(backend, data_dir=data_dir, options=options)
    if backend in {"builtin.read", "read", "Read"}:
        return create_read_tool(
            repo_root=repo_root,
            data_dir=data_dir,
            memory_space_dir=memory_space_dir,
            options=options,
        )
    if backend in {"builtin.bash", "bash", "Bash"}:
        return create_bash_tool(
            repo_root=repo_root,
            data_dir=data_dir,
            memory_space_dir=memory_space_dir,
            options=options,
        )
    if backend in {"builtin.write", "write", "Write"}:
        return create_write_tool(
            repo_root=repo_root,
            data_dir=data_dir,
            memory_space_dir=memory_space_dir,
            options=options,
        )
    if backend in {"builtin.edit", "edit", "Edit"}:
        return create_edit_tool(
            repo_root=repo_root,
            data_dir=data_dir,
            memory_space_dir=memory_space_dir,
            options=options,
        )
    if backend in {"builtin.web_search", "web_search"}:
        return create_web_search_tool()
    if backend in {"builtin.get_weather", "get_weather", "weather"}:
        return create_weather_tool()
    if backend in {"builtin.agent", "agent"} and subagent_host is not None:
        return create_agent_tool(subagent_host)
    if backend in {"builtin.ask_user", "ask_user"} and question_channel is not None:
        return create_ask_user_tool(question_channel)
    raise ValueError(f"Unknown or unavailable tool backend: {backend}")
