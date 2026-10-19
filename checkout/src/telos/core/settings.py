"""Configuration loading."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from telos.core.coerce import coerce_bool
from telos.core.errors import ConfigurationError
from telos.core.retry import RetryPolicy, parse_retry_policy
from telos.storage.paths import TelosPaths, clean_memory_space_name, find_repo_root


REMOVED_CONFIG_FIELDS = frozenset(
    {
        "workspace",
        "workspace_dir",
        "home_dir",
        "data_dir",
        "data_root_dir",
        "namespace",
        "namespace_dir",
        "conversations_dir",
        "memory_spaces",
        "read_memory_space",
        "write_memory_space",
        "legacy_hooks",
    }
)
BACKGROUND_RETRY_MAX_ATTEMPTS = 5
TIMELINE_SUMMARY_MAX_RETRY_DEFAULT = 3
TIMELINE_SUMMARY_TIMEOUT_SECONDS_DEFAULT = 120.0


@dataclass(frozen=True)
class AppSettings:
    id: str = "default"
    name: str = "telos"
    description: str = ""
    memory_space: str = "defaults"


@dataclass(frozen=True)
class AdapterSettings:
    enabled: list[str] = field(default_factory=lambda: ["cli"])
    tool_receipts: object | None = None


@dataclass(frozen=True)
class PolicyHookSettings:
    mode: str = "manual"


@dataclass(frozen=True)
class HookSettings:
    policy: PolicyHookSettings = field(default_factory=PolicyHookSettings)


@dataclass(frozen=True)
class RecapSettings:
    enabled: bool = True
    idle_seconds: int = 0
    min_messages: int = 10
    retry: RetryPolicy = field(
        default_factory=lambda: RetryPolicy(max_attempts=BACKGROUND_RETRY_MAX_ATTEMPTS)
    )
    prompt_template: str = ""
    prompts_placeholder: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MemorySettings:
    recap: RecapSettings = field(default_factory=RecapSettings)


@dataclass(frozen=True)
class AdapterPromptInjectionEntry:
    prompt: str = ""
    text: str = ""
    enabled: bool = True


@dataclass(frozen=True)
class AdapterPromptInjectionSettings:
    enabled: bool = False
    prompts: dict[str, AdapterPromptInjectionEntry] = field(default_factory=dict)


@dataclass(frozen=True)
class SystemPromptSettings:
    enabled: bool = True
    prompt_template: str = ""
    prompts_placeholder: dict[str, str] = field(default_factory=dict)
    adapter_prompt_injection: AdapterPromptInjectionSettings = field(
        default_factory=AdapterPromptInjectionSettings
    )


@dataclass(frozen=True)
class AgentLoopSettings:
    max_steps: int = 20
    hooks: HookSettings = field(default_factory=HookSettings)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    cache: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TimelineSummarySettings:
    enabled: bool = False
    check_interval_seconds: int = 5
    max_concurrency: int = 2
    max_captures_per_batch: int = 20
    max_retry: int = TIMELINE_SUMMARY_MAX_RETRY_DEFAULT
    timeout_seconds: float = TIMELINE_SUMMARY_TIMEOUT_SECONDS_DEFAULT
    provider: str = ""
    retry: RetryPolicy = field(
        default_factory=lambda: RetryPolicy(max_attempts=TIMELINE_SUMMARY_MAX_RETRY_DEFAULT)
    )
    prompt_template: str = ""


@dataclass(frozen=True)
class TimelineCollectorSettings:
    enabled: bool = False
    poll_interval_seconds: float = 1.0
    quiet_gap_seconds: float = 10.0
    min_capture_interval_seconds: float = 1.0
    settle_seconds: float = 0.3
    max_depth: int = 8
    max_nodes: int = 600
    max_text_chars: int = 3000
    compact_text_chars: int = 12000
    include_tree: bool = True
    screenshot_max_long_edge: int = 1568
    jpeg_quality: int = 85
    debug_persist_duplicate_captures: bool = False
    exclude_bundle_prefixes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TimelineSettings:
    enabled: bool = False
    collector: TimelineCollectorSettings = field(default_factory=TimelineCollectorSettings)
    summary: TimelineSummarySettings = field(default_factory=TimelineSummarySettings)


@dataclass(frozen=True)
class ActivityAssignSettings:
    """快环路。参数含义见 `tasks/activity-graph-v2.md` §9.1。"""

    enabled: bool = False
    check_interval_seconds: int = 60
    window_seconds: int = 300
    max_details_per_run: int = 200
    pool_limit: int = 6
    max_retry: int = TIMELINE_SUMMARY_MAX_RETRY_DEFAULT
    timeout_seconds: float = TIMELINE_SUMMARY_TIMEOUT_SECONDS_DEFAULT
    provider: str = ""
    retry: RetryPolicy = field(
        default_factory=lambda: RetryPolicy(max_attempts=TIMELINE_SUMMARY_MAX_RETRY_DEFAULT)
    )
    prompt_template: str = ""


@dataclass(frozen=True)
class ActivityConsolidateSettings:
    """慢环路。参数含义见 `tasks/activity-graph-v2.md` §9.1。"""

    enabled: bool = False
    interval_seconds: int = 3600
    lookback_seconds: int = 14400
    edit_window_seconds: int = 86400
    max_retry: int = TIMELINE_SUMMARY_MAX_RETRY_DEFAULT
    # 一次复核要读完窗口内全部活动的全部片段，比单批 timeline 总结长得多。
    # 实测 4 小时窗口 6 条活动约 105 秒，日终全天一轮更久，留出余量。
    timeout_seconds: float = 600.0
    provider: str = ""
    retry: RetryPolicy = field(
        default_factory=lambda: RetryPolicy(max_attempts=TIMELINE_SUMMARY_MAX_RETRY_DEFAULT)
    )
    prompt_template: str = ""


@dataclass(frozen=True)
class ActivitySettings:
    enabled: bool = False
    seal_idle_seconds: int = 7200
    min_uncovered_seconds: int = 1200
    assign: ActivityAssignSettings = field(default_factory=ActivityAssignSettings)
    consolidate: ActivityConsolidateSettings = field(
        default_factory=ActivityConsolidateSettings
    )


@dataclass(frozen=True)
class DaemonSettings:
    timeline: TimelineSettings = field(default_factory=TimelineSettings)
    activity: ActivitySettings = field(default_factory=ActivitySettings)


@dataclass(frozen=True)
class ProviderEntry:
    name: str
    type: str
    model: str = ""
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderSettings:
    entries: dict[str, ProviderEntry] = field(default_factory=dict)
    default: str = "fake"


@dataclass(frozen=True)
class ToolPolicySettings:
    mode: str = "custom"


@dataclass(frozen=True)
class ToolEntrySettings:
    name: str
    enabled: bool
    approval: bool | None
    backend: str = ""
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSettings:
    enabled: list[str] = field(default_factory=list)
    schema: list["ToolDefinition"] = field(default_factory=list)
    policy: ToolPolicySettings = field(default_factory=ToolPolicySettings)
    entries: dict[str, ToolEntrySettings] = field(default_factory=dict)

    @property
    def approvals(self) -> dict[str, bool]:
        return {
            name: entry.approval
            for name, entry in self.entries.items()
            if entry.enabled and entry.approval is not None
        }


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    backend: str
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SkillSettings:
    enabled: bool = True
    roots: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class McpSettings:
    enabled: bool = True
    servers: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TelosSettings:
    enabled_agents: list[str]
    agent_config: dict[str, Any]
    app: AppSettings
    adapters: AdapterSettings
    agent_loop: AgentLoopSettings
    daemon: DaemonSettings
    system_prompt: SystemPromptSettings
    memory: MemorySettings
    providers: ProviderSettings
    tools: ToolSettings
    skills: SkillSettings
    mcp: McpSettings
    paths: TelosPaths
    assistants: dict[str, Any] = field(default_factory=dict)


def load_settings(
    config_dir: str | Path = "configs",
    *,
    repo_root: str | Path | None = None,
    agent_id: str | None = None,
) -> TelosSettings:
    root = Path(repo_root).resolve() if repo_root is not None else find_repo_root()
    main_config_path = _resolve_config_path(root, Path(config_dir))
    root_dir = _default_telos_root()
    telos_raw = _load_config_file(main_config_path)
    _reject_removed_config_fields(telos_raw, path=main_config_path, section="root")
    _reject_root_tools_field(telos_raw, path=main_config_path)
    enabled_agents = _parse_enabled_agents(telos_raw)
    selected_agent_id = _select_agent_id(
        enabled_agents,
        telos_raw,
        requested=agent_id,
    )
    agent_raw, agent_config_path = _load_agent_config(
        telos_raw,
        selected_agent_id,
        root=root,
        main_config_path=main_config_path,
    )
    _reject_removed_config_fields(
        agent_raw,
        path=agent_config_path,
        section=f"agents.{selected_agent_id}",
    )
    app = _parse_app(
        agent_raw,
        agent_id=selected_agent_id,
        config_path=agent_config_path,
    )
    paths = TelosPaths.from_root(
        root,
        config_dir=main_config_path.parent,
        config_path=agent_config_path,
        root_dir=root_dir,
        main_config_path=main_config_path,
        agent_id=selected_agent_id,
        agent_config_path=agent_config_path,
        memory_space=app.memory_space,
    )
    return TelosSettings(
        enabled_agents=enabled_agents,
        agent_config=agent_raw,
        app=app,
        adapters=_parse_adapters(agent_raw.get("adapters", {})),
        agent_loop=_parse_agent_loop(agent_raw.get("agent_loop", {})),
        daemon=_parse_daemon(agent_raw.get("daemon", {}), root=root),
        system_prompt=_parse_system_prompt(agent_raw.get("system_prompt", {}), root=root),
        memory=_parse_memory(agent_raw.get("memory"), root=root),
        providers=_parse_providers_config(agent_raw.get("providers", {})),
        tools=_parse_tools(
            agent_raw.get("tools", {}),
            config_base_dir=agent_config_path.parent,
        ),
        skills=_parse_skills(agent_raw.get("skills", {}), root=root),
        mcp=_parse_mcp(_mcp_section(agent_raw), root=root),
        paths=paths,
        assistants=_as_dict(agent_raw.get("assistants")),
    )


def load_enabled_agent_settings(
    config_dir: str | Path = "configs",
    *,
    repo_root: str | Path | None = None,
) -> list[TelosSettings]:
    root = Path(repo_root).resolve() if repo_root is not None else find_repo_root()
    main_config_path = _resolve_config_path(root, Path(config_dir))
    telos_raw = _load_config_file(main_config_path)
    _reject_removed_config_fields(telos_raw, path=main_config_path, section="root")
    _reject_root_tools_field(telos_raw, path=main_config_path)
    return [
        load_settings(main_config_path, repo_root=root, agent_id=agent_id)
        for agent_id in _parse_enabled_agents(telos_raw)
    ]


def _resolve_config_path(root: Path, config_path: Path) -> Path:
    resolved = config_path.expanduser()
    if not resolved.is_absolute():
        resolved = root / resolved
    resolved = resolved.resolve()
    if resolved.is_file() or resolved.suffix in {".yaml", ".yml", ".json"}:
        return resolved
    return resolved / "telos.yaml"


def _load_config_file(path: Path) -> dict[str, Any]:
    return load_config_mapping(path)


def load_config_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        data = json.loads(text)
    else:
        data = _parse_simple_yaml(text)
    if not isinstance(data, dict):
        raise ConfigurationError(f"Config file {path} must contain an object")
    return data


def _reject_removed_config_fields(raw: dict[str, Any], *, path: Path, section: str) -> None:
    removed = sorted(field for field in raw if field in REMOVED_CONFIG_FIELDS)
    if removed:
        fields = ", ".join(removed)
        raise ConfigurationError(
            f"Removed config field(s) in {section} of {path}: {fields}"
        )


def _reject_root_tools_field(raw: dict[str, Any], *, path: Path) -> None:
    if "tools" in raw:
        raise ConfigurationError(
            f"tools must be configured inside each agent, not at the root of {path}"
        )


def _parse_enabled_agents(raw: dict[str, Any]) -> list[str]:
    enabled = [
        _clean_agent_id(item)
        for item in _as_list(raw.get("enabled_agents"))
        if str(item or "").strip()
    ]
    if not enabled:
        raise ConfigurationError("enabled_agents must contain at least one agent id")
    agents = _as_dict(raw.get("agents"))
    missing = [agent_id for agent_id in enabled if agent_id not in agents]
    if missing:
        raise ConfigurationError(f"enabled_agents references unknown agents: {', '.join(missing)}")
    return enabled


def _select_agent_id(
    enabled_agents: list[str],
    raw: dict[str, Any],
    *,
    requested: str | None,
) -> str:
    if requested is None or not str(requested).strip():
        return enabled_agents[0]
    agent_id = _clean_agent_id(requested)
    agents = _as_dict(raw.get("agents"))
    if agent_id not in agents:
        raise ConfigurationError(f"Agent {agent_id!r} is not configured")
    return agent_id


def _load_agent_config(
    raw: dict[str, Any],
    agent_id: str,
    *,
    root: Path,
    main_config_path: Path,
) -> tuple[dict[str, Any], Path]:
    agents = _as_dict(raw.get("agents"))
    spec = agents.get(agent_id)
    if isinstance(spec, str):
        path = _absolute_config_path(spec, field=f"agents.{agent_id}", root=root)
        data = load_config_mapping(path)
        return data, path
    if isinstance(spec, dict):
        return dict(spec), main_config_path
    raise ConfigurationError(f"agents.{agent_id} must be a path or object")


def _parse_app(raw: Any, *, agent_id: str, config_path: Path) -> AppSettings:
    data = _as_dict(raw)
    name = str(data.get("name") or agent_id or config_path.stem).strip()
    memory_space = _memory_space_name(data.get("memory_space") or "defaults")
    return AppSettings(
        id=agent_id,
        name=name,
        description=str(data.get("description") or ""),
        memory_space=memory_space,
    )


def _parse_adapters(raw: Any) -> AdapterSettings:
    data = _as_dict(raw)
    return AdapterSettings(
        enabled=[str(item) for item in _as_list(data.get("enabled"))],
        tool_receipts=data.get("tool_receipts"),
    )


def _parse_agent_loop(raw: Any) -> AgentLoopSettings:
    data = _as_dict(raw)
    if "hooks" in data:
        raise ConfigurationError("agent_loop.hooks.policy has moved to tools.policy")
    return AgentLoopSettings(
        max_steps=int(data.get("max_steps") or 20),
        retry=parse_retry_policy(data.get("retry"), field_prefix="agent_loop.retry"),
        cache=_as_dict(data.get("cache")),
    )


def _parse_daemon(raw: Any, *, root: Path) -> DaemonSettings:
    data = _as_dict(raw)
    if "friends_maintenance" in data:
        raise ConfigurationError(
            "Removed config field(s) in daemon: friends_maintenance"
        )
    return DaemonSettings(
        timeline=_parse_timeline(data.get("timeline"), root=root),
        activity=_parse_activity(data.get("activity"), root=root),
    )


def _parse_activity(raw: Any, *, root: Path) -> ActivitySettings:
    if raw is None:
        return ActivitySettings()
    data = _as_dict(raw)
    enabled = coerce_bool(data.get("enabled"), default=False)
    return ActivitySettings(
        enabled=enabled,
        seal_idle_seconds=_coerce_positive_int(
            data.get("seal_idle_seconds"),
            field="daemon.activity.seal_idle_seconds",
            default=7200,
        ),
        min_uncovered_seconds=_coerce_positive_int(
            data.get("min_uncovered_seconds"),
            field="daemon.activity.min_uncovered_seconds",
            default=1200,
        ),
        assign=_parse_activity_assign(
            data.get("assign"),
            enabled_default=enabled,
            root=root,
        ),
        consolidate=_parse_activity_consolidate(
            data.get("consolidate"),
            enabled_default=enabled,
            root=root,
        ),
    )


def _parse_activity_assign(
    raw: Any,
    *,
    enabled_default: bool,
    root: Path,
) -> ActivityAssignSettings:
    data = _as_dict(raw)
    retry, max_retry = _parse_activity_retry(data, field_prefix="daemon.activity.assign")
    return ActivityAssignSettings(
        enabled=coerce_bool(data.get("enabled"), default=enabled_default),
        check_interval_seconds=_coerce_positive_int(
            data.get("check_interval_seconds"),
            field="daemon.activity.assign.check_interval_seconds",
            default=60,
        ),
        window_seconds=_coerce_positive_int(
            data.get("window_seconds"),
            field="daemon.activity.assign.window_seconds",
            default=300,
        ),
        max_details_per_run=_coerce_positive_int(
            data.get("max_details_per_run"),
            field="daemon.activity.assign.max_details_per_run",
            default=200,
        ),
        pool_limit=_coerce_positive_int(
            data.get("pool_limit"),
            field="daemon.activity.assign.pool_limit",
            default=6,
        ),
        max_retry=max_retry,
        timeout_seconds=_coerce_positive_float(
            data.get("timeout_seconds"),
            field="daemon.activity.assign.timeout_seconds",
            default=TIMELINE_SUMMARY_TIMEOUT_SECONDS_DEFAULT,
        ),
        provider=str(data.get("provider") or "").strip(),
        retry=retry,
        prompt_template=_optional_absolute_config_path(
            data.get("prompt_template"),
            field="daemon.activity.assign.prompt_template",
            root=root,
        ),
    )


def _parse_activity_consolidate(
    raw: Any,
    *,
    enabled_default: bool,
    root: Path,
) -> ActivityConsolidateSettings:
    data = _as_dict(raw)
    retry, max_retry = _parse_activity_retry(data, field_prefix="daemon.activity.consolidate")
    return ActivityConsolidateSettings(
        enabled=coerce_bool(data.get("enabled"), default=enabled_default),
        interval_seconds=_coerce_positive_int(
            data.get("interval_seconds"),
            field="daemon.activity.consolidate.interval_seconds",
            default=3600,
        ),
        lookback_seconds=_coerce_positive_int(
            data.get("lookback_seconds"),
            field="daemon.activity.consolidate.lookback_seconds",
            default=14400,
        ),
        edit_window_seconds=_coerce_positive_int(
            data.get("edit_window_seconds"),
            field="daemon.activity.consolidate.edit_window_seconds",
            default=86400,
        ),
        max_retry=max_retry,
        timeout_seconds=_coerce_positive_float(
            data.get("timeout_seconds"),
            field="daemon.activity.consolidate.timeout_seconds",
            default=600.0,
        ),
        provider=str(data.get("provider") or "").strip(),
        retry=retry,
        prompt_template=_optional_absolute_config_path(
            data.get("prompt_template"),
            field="daemon.activity.consolidate.prompt_template",
            root=root,
        ),
    )


def _parse_activity_retry(
    data: dict[str, Any],
    *,
    field_prefix: str,
) -> tuple[RetryPolicy, int]:
    retry = parse_retry_policy(
        data.get("retry"),
        field_prefix=f"{field_prefix}.retry",
        default_max_attempts=TIMELINE_SUMMARY_MAX_RETRY_DEFAULT,
    )
    if "max_retry" not in data:
        return retry, retry.max_attempts
    max_retry = _coerce_positive_int(
        data.get("max_retry"),
        field=f"{field_prefix}.max_retry",
        default=TIMELINE_SUMMARY_MAX_RETRY_DEFAULT,
    )
    return replace(retry, max_attempts=max_retry), max_retry


def _parse_timeline(raw: Any, *, root: Path) -> TimelineSettings:
    if raw is None:
        return TimelineSettings()
    data = _as_dict(raw)
    if "db_path" in data:
        raise ConfigurationError(
            "Removed config field(s) in daemon.timeline: db_path"
        )
    enabled = coerce_bool(data.get("enabled"), default=False)
    return TimelineSettings(
        enabled=enabled,
        collector=_parse_timeline_collector(data.get("collector"), enabled_default=enabled),
        summary=_parse_timeline_summary(
            data.get("summary"),
            enabled_default=enabled,
            root=root,
        ),
    )


def _parse_timeline_collector(
    raw: Any,
    *,
    enabled_default: bool,
) -> TimelineCollectorSettings:
    data = _as_dict(raw)
    return TimelineCollectorSettings(
        enabled=coerce_bool(data.get("enabled"), default=enabled_default),
        poll_interval_seconds=_coerce_non_negative_float(
            data.get("poll_interval_seconds"),
            field="daemon.timeline.collector.poll_interval_seconds",
            default=1.0,
        ),
        quiet_gap_seconds=_coerce_non_negative_float(
            data.get("quiet_gap_seconds"),
            field="daemon.timeline.collector.quiet_gap_seconds",
            default=10.0,
        ),
        min_capture_interval_seconds=_coerce_non_negative_float(
            data.get("min_capture_interval_seconds"),
            field="daemon.timeline.collector.min_capture_interval_seconds",
            default=1.0,
        ),
        settle_seconds=_coerce_non_negative_float(
            data.get("settle_seconds"),
            field="daemon.timeline.collector.settle_seconds",
            default=0.3,
        ),
        max_depth=_coerce_non_negative_int(
            data.get("max_depth"),
            field="daemon.timeline.collector.max_depth",
            default=8,
        ),
        max_nodes=_coerce_positive_int(
            data.get("max_nodes"),
            field="daemon.timeline.collector.max_nodes",
            default=600,
        ),
        max_text_chars=_coerce_positive_int(
            data.get("max_text_chars"),
            field="daemon.timeline.collector.max_text_chars",
            default=3000,
        ),
        compact_text_chars=_coerce_positive_int(
            data.get("compact_text_chars"),
            field="daemon.timeline.collector.compact_text_chars",
            default=12000,
        ),
        include_tree=coerce_bool(data.get("include_tree"), default=True),
        screenshot_max_long_edge=_coerce_positive_int(
            data.get("screenshot_max_long_edge"),
            field="daemon.timeline.collector.screenshot_max_long_edge",
            default=1568,
        ),
        jpeg_quality=_coerce_positive_int(
            data.get("jpeg_quality"),
            field="daemon.timeline.collector.jpeg_quality",
            default=85,
        ),
        debug_persist_duplicate_captures=coerce_bool(
            data.get("debug_persist_duplicate_captures"),
            default=False,
        ),
        exclude_bundle_prefixes=[
            str(item)
            for item in _as_list(data.get("exclude_bundle_prefixes"))
            if str(item or "").strip()
        ],
    )


def _parse_timeline_summary(
    raw: Any,
    *,
    enabled_default: bool,
    root: Path,
) -> TimelineSummarySettings:
    data = _as_dict(raw)
    removed = [field for field in ("output_dir", "interval_seconds") if field in data]
    if removed:
        raise ConfigurationError(
            "Removed config field(s) in daemon.timeline.summary: " + ", ".join(removed)
        )
    retry = parse_retry_policy(
        data.get("retry"),
        field_prefix="daemon.timeline.summary.retry",
        default_max_attempts=TIMELINE_SUMMARY_MAX_RETRY_DEFAULT,
    )
    if "max_retry" in data:
        max_retry = _coerce_positive_int(
            data.get("max_retry"),
            field="daemon.timeline.summary.max_retry",
            default=TIMELINE_SUMMARY_MAX_RETRY_DEFAULT,
        )
        retry = replace(retry, max_attempts=max_retry)
    else:
        max_retry = retry.max_attempts
    return TimelineSummarySettings(
        enabled=coerce_bool(data.get("enabled"), default=enabled_default),
        check_interval_seconds=_coerce_positive_int(
            data.get("check_interval_seconds"),
            field="daemon.timeline.summary.check_interval_seconds",
            default=5,
        ),
        max_concurrency=_coerce_positive_int(
            data.get("max_concurrency"),
            field="daemon.timeline.summary.max_concurrency",
            default=2,
        ),
        max_captures_per_batch=_coerce_positive_int(
            data.get("max_captures_per_batch"),
            field="daemon.timeline.summary.max_captures_per_batch",
            default=20,
        ),
        max_retry=max_retry,
        timeout_seconds=_coerce_positive_float(
            data.get("timeout_seconds"),
            field="daemon.timeline.summary.timeout_seconds",
            default=TIMELINE_SUMMARY_TIMEOUT_SECONDS_DEFAULT,
        ),
        provider=str(data.get("provider") or "").strip(),
        retry=retry,
        prompt_template=_optional_absolute_config_path(
            data.get("prompt_template"),
            field="daemon.timeline.summary.prompt_template",
            root=root,
        ),
    )


def _parse_system_prompt(raw: Any, *, root: Path) -> SystemPromptSettings:
    data = _as_dict(raw)
    return SystemPromptSettings(
        enabled=coerce_bool(data.get("enabled"), default=True),
        prompt_template=_optional_absolute_config_path(
            data.get("prompt_template"),
            field="system_prompt.prompt_template",
            root=root,
        ),
        prompts_placeholder=_parse_prompts_placeholder(
            data.get("prompts_placeholder"),
            field_prefix="system_prompt.prompts_placeholder",
            root=root,
        ),
        adapter_prompt_injection=_parse_adapter_prompt_injection(
            data.get("adapter_prompt_injection"),
            root=root,
        ),
    )


def _parse_prompts_placeholder(raw: Any, *, field_prefix: str, root: Path) -> dict[str, str]:
    if raw is None:
        return {}
    data = _as_dict(raw)
    return {
        str(key).strip(): _required_absolute_config_path(
            value,
            field=f"{field_prefix}.{key}",
            root=root,
        )
        for key, value in data.items()
    }


def _parse_adapter_prompt_injection(raw: Any, *, root: Path) -> AdapterPromptInjectionSettings:
    if raw is None:
        return AdapterPromptInjectionSettings(enabled=False)
    if not isinstance(raw, dict):
        return AdapterPromptInjectionSettings(
            enabled=coerce_bool(raw, default=True),
        )
    data = _as_dict(raw)
    if not data:
        return AdapterPromptInjectionSettings(enabled=False)
    if "prompts" in data or "adapters" in data or "enabled" in data:
        raw_prompts = data.get("prompts")
        if raw_prompts is None:
            raw_prompts = data.get("adapters")
        enabled = coerce_bool(data.get("enabled"), default=True)
    else:
        raw_prompts = data
        enabled = True
    prompts: dict[str, AdapterPromptInjectionEntry] = {}
    for adapter, value in _as_dict(raw_prompts).items():
        key = str(adapter or "").strip().lower()
        if not key:
            continue
        if isinstance(value, bool):
            if value:
                raise ConfigurationError(
                    f"system_prompt.adapter_prompt_injection.{key} must set text or an absolute prompt path"
                )
            prompts[key] = AdapterPromptInjectionEntry(enabled=value)
            continue
        if isinstance(value, str):
            prompts[key] = AdapterPromptInjectionEntry(
                prompt=_required_absolute_config_path(
                    value,
                    field=f"system_prompt.adapter_prompt_injection.{key}",
                    root=root,
                )
            )
            continue
        if value is None:
            raise ConfigurationError(
                f"system_prompt.adapter_prompt_injection.{key} must set text or an absolute prompt path"
            )
        item = _as_dict(value)
        prompt = str(item.get("prompt") or "").strip()
        text = str(item.get("text") or "").strip()
        if prompt and text:
            raise ConfigurationError(
                f"system_prompt.adapter_prompt_injection.{key} cannot set both text and prompt"
            )
        if prompt:
            prompt = _required_absolute_config_path(
                prompt,
                field=f"system_prompt.adapter_prompt_injection.{key}.prompt",
                root=root,
            )
        elif not text and coerce_bool(item.get("enabled"), default=True):
            raise ConfigurationError(
                f"system_prompt.adapter_prompt_injection.{key} must set text or an absolute prompt path"
            )
        prompts[key] = AdapterPromptInjectionEntry(
            prompt=prompt,
            text=text,
            enabled=coerce_bool(item.get("enabled"), default=True),
        )
    return AdapterPromptInjectionSettings(
        enabled=enabled,
        prompts=prompts,
    )


def _parse_memory(raw: Any, *, root: Path) -> MemorySettings:
    if isinstance(raw, dict):
        data = _as_dict(raw)
        if "curation" in data:
            raise ConfigurationError(
                "memory.curation has been removed; future automatic memory review belongs to dream"
            )
        return MemorySettings(
            recap=_parse_recap(data.get("recap"), root=root),
        )
    if raw is None:
        return MemorySettings()
    raise ConfigurationError("memory must be an object")


def _parse_recap(raw: Any, *, root: Path, default: bool | None = None) -> RecapSettings:
    enabled_default = True if default is None else default
    if isinstance(raw, dict):
        data = _as_dict(raw)
        return RecapSettings(
            enabled=coerce_bool(data.get("enabled"), default=enabled_default),
            idle_seconds=_coerce_non_negative_int(
                data.get("idle_seconds"),
                field="memory.recap.idle_seconds",
                default=0,
            ),
            min_messages=_coerce_non_negative_int(
                data.get("min_messages"),
                field="memory.recap.min_messages",
                default=10,
            ),
            retry=parse_retry_policy(
                data.get("retry"),
                field_prefix="memory.recap.retry",
                default_max_attempts=BACKGROUND_RETRY_MAX_ATTEMPTS,
            ),
            prompt_template=_optional_absolute_config_path(
                data.get("prompt_template"),
                field="memory.recap.prompt_template",
                root=root,
            ),
            prompts_placeholder=_parse_prompts_placeholder(
                data.get("prompts_placeholder"),
                field_prefix="memory.recap.prompts_placeholder",
                root=root,
            ),
        )
    if raw is None:
        return RecapSettings(enabled=enabled_default)
    return RecapSettings(enabled=coerce_bool(raw, default=enabled_default))


def _parse_providers_config(raw: Any) -> ProviderSettings:
    data = _as_dict(raw)
    if "providers" in data:
        providers = dict(_as_dict(data.get("providers")))
        default_name = str(data.get("default") or providers.pop("default", "") or "").strip()
    else:
        providers = {key: value for key, value in data.items() if key != "default"}
        default_name = str(data.get("default") or "").strip()
    return _parse_providers(providers, default_name=default_name)


def _parse_providers(raw: Any, *, default_name: str = "") -> ProviderSettings:
    data = _as_dict(raw)
    entries: dict[str, ProviderEntry] = {}
    for name, value in data.items():
        item = _as_dict(value)
        provider_type = str(item.get("type") or name)
        model = str(item.get("model") or "")
        options = {key: val for key, val in item.items() if key not in {"type", "model"}}
        entries[str(name)] = ProviderEntry(
            name=str(name),
            type=provider_type,
            model=model,
            options=options,
        )
    if "fake" not in entries:
        entries["fake"] = ProviderEntry(name="fake", type="fake", model="fake-echo")
    if default_name and default_name not in entries:
        raise ConfigurationError(f"Default provider {default_name!r} is not configured")
    default = default_name or ("fake" if "fake" in entries else next(iter(entries), "fake"))
    return ProviderSettings(entries=entries, default=default)


def _parse_tools(raw: Any, *, config_base_dir: Path) -> ToolSettings:
    del config_base_dir
    data = _as_dict(raw)
    _reject_unknown_tool_fields(data, field="tools")
    policy = _parse_tool_policy(data.get("policy"))
    entries_raw = _as_dict(data.get("entries"))
    entries: dict[str, ToolEntrySettings] = {}
    schema: list[ToolDefinition] = []
    enabled: list[str] = []
    for raw_name, raw_entry in entries_raw.items():
        name = str(raw_name or "").strip()
        if not name:
            raise ConfigurationError("tools.entries contains an empty tool name")
        if "*" in name:
            raise ConfigurationError(
                f"tools.entries.{name} must use an exact tool name; wildcards are not supported"
            )
        entry = _parse_tool_entry(name, raw_entry)
        entries[name] = entry
        if entry.enabled:
            enabled.append(name)
        if entry.backend:
            schema.append(
                ToolDefinition(
                    name=name,
                    backend=entry.backend,
                    description=entry.description,
                    parameters=entry.parameters,
                    options=entry.options,
                )
            )
    return ToolSettings(
        enabled=enabled,
        schema=schema,
        policy=policy,
        entries=entries,
    )


def _parse_tool_policy(raw: Any) -> ToolPolicySettings:
    data = _as_dict(raw)
    unknown = sorted(str(key) for key in data if key != "mode")
    if unknown:
        raise ConfigurationError(
            f"tools.policy only supports mode; unknown field(s): {', '.join(unknown)}"
        )
    mode = str(data.get("mode") or "custom").strip().lower()
    if mode not in {"always_ask", "custom", "yolo"}:
        raise ConfigurationError(
            "tools.policy.mode must be always_ask, custom or yolo"
        )
    return ToolPolicySettings(mode=mode)


def _parse_tool_entry(name: str, raw: Any) -> ToolEntrySettings:
    data = _as_dict(raw)
    allowed = {
        "enabled",
        "approval",
        "backend",
        "description",
        "parameters",
        "required",
        "options",
    }
    unknown = sorted(str(key) for key in data if key not in allowed)
    if unknown:
        raise ConfigurationError(
            f"tools.entries.{name} has unknown field(s): {', '.join(unknown)}"
        )
    if "enabled" not in data or not isinstance(data.get("enabled"), bool):
        raise ConfigurationError(f"tools.entries.{name}.enabled must be boolean")
    if "approval" not in data or (data["approval"] is not None and not isinstance(data["approval"], bool)):
        raise ConfigurationError(f"tools.entries.{name}.approval must be boolean or null")
    return ToolEntrySettings(
        name=name,
        enabled=bool(data["enabled"]),
        approval=data["approval"],
        backend=str(data.get("backend") or "").strip(),
        description=str(data.get("description") or "").strip(),
        parameters=_parse_tool_parameters(data),
        options=_as_dict(data.get("options")),
    )


def _reject_unknown_tool_fields(data: dict[str, Any], *, field: str) -> None:
    allowed = {"policy", "entries"}
    unknown = sorted(str(key) for key in data if key not in allowed)
    if unknown:
        raise ConfigurationError(
            f"{field} only supports policy and entries; "
            f"unknown field(s): {', '.join(unknown)}"
        )


def parse_tool_settings(raw: Any, *, config_base_dir: Path) -> ToolSettings:
    return _parse_tools(raw, config_base_dir=config_base_dir)


def resolve_tool_definition_placeholders(
    definition: ToolDefinition,
    *,
    repo_root: Path,
    data_dir: Path | None,
    memory_space_dir: Path | None,
) -> ToolDefinition:
    return ToolDefinition(
        name=definition.name,
        backend=definition.backend,
        description=definition.description,
        parameters=definition.parameters,
        options=_resolve_tool_placeholders(
            definition.options,
            {
                "repo_root": repo_root,
                "data_dir": data_dir,
                "memory_space_dir": memory_space_dir,
            },
        ),
    )


def _resolve_tool_placeholders(value: Any, values: dict[str, Path | None]) -> Any:
    if isinstance(value, dict):
        return {
            key: _resolve_tool_placeholders(item, values)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_resolve_tool_placeholders(item, values) for item in value]
    if not isinstance(value, str):
        return value
    text = value
    for name, path in values.items():
        placeholder = "{" + name + "}"
        if placeholder not in text:
            continue
        if path is None:
            raise ConfigurationError(
                f"tool schema options use {placeholder}, but it is not configured"
            )
        text = text.replace(placeholder, str(path))
    return text


def _parse_tool_parameters(data: dict[str, Any]) -> dict[str, Any]:
    raw_parameters = data.get("parameters")
    parameters = _as_dict(raw_parameters) if raw_parameters is not None else {}
    raw_required = data.get("required")
    inline_required = parameters.get("required")
    required = _required_list(
        raw_required if raw_required is not None else inline_required
    )

    if _looks_like_json_schema(parameters):
        schema = dict(parameters)
        if raw_required is not None:
            schema["required"] = required
        return schema

    schema = {
        "type": "object",
        "properties": {
            key: value for key, value in parameters.items() if key != "required"
        },
    }
    if required:
        schema["required"] = required
    return schema


def _looks_like_json_schema(parameters: dict[str, Any]) -> bool:
    if isinstance(parameters.get("type"), str):
        return True
    return any(
        key in parameters
        for key in {
            "$schema",
            "$defs",
            "allOf",
            "anyOf",
            "oneOf",
            "properties",
        }
    )


def _required_list(value: Any) -> list[str]:
    if value is None:
        return []
    return [str(item) for item in _as_list(value)]


def _parse_skills(raw: Any, *, root: Path) -> SkillSettings:
    data = _as_dict(raw)
    roots = [
        _required_absolute_config_path(item, field="skills.roots", root=root)
        for item in _as_list(data.get("roots"))
    ]
    return SkillSettings(
        enabled=coerce_bool(data.get("enabled"), default=True),
        roots=roots,
    )


def _parse_mcp(raw: Any, *, root: Path) -> McpSettings:
    data = _as_dict(raw)
    if "mcp" in data:
        data = _as_dict(data.get("mcp"))
    return McpSettings(
        enabled=coerce_bool(data.get("enabled"), default=True),
        servers=_normalize_mcp_servers(
            _as_dict(data.get("mcpServers") if "mcpServers" in data else data.get("servers")),
            root=root,
        ),
    )


def _mcp_section(root_raw: dict[str, Any]) -> Any:
    if "mcp" in root_raw:
        return root_raw.get("mcp")
    return {}


def _normalize_mcp_servers(servers: dict[str, Any], *, root: Path) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for name, raw in servers.items():
        if not isinstance(raw, dict):
            normalized[str(name)] = raw
            continue
        server = dict(raw)
        if server.get("cwd") not in (None, ""):
            server["cwd"] = _required_absolute_config_path(
                server.get("cwd"),
                field=f"mcp.servers.{name}.cwd",
                root=root,
            )
        normalized[str(name)] = server
    return normalized


def _memory_space_name(value: object) -> str:
    try:
        return clean_memory_space_name(value)
    except ValueError as error:
        raise ConfigurationError(str(error)) from error


def _optional_absolute_config_path(value: object, *, field: str, root: Path) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return _required_absolute_config_path(text, field=field, root=root)


def _required_absolute_config_path(value: object, *, field: str, root: Path) -> str:
    return str(_absolute_config_path(value, field=field, root=root))


def _absolute_config_path(value: object, *, field: str, root: Path) -> Path:
    # 相对路径按配置所属的仓库根解析。仓库自带的 configs/ 会被打进 App，也会被
    # 持续集成检出到别的目录，写死绝对路径在本机之外一律失效。用户配置里的绝对
    # 路径行为不变。
    text = _resolve_braced_env(str(value or "").strip())
    if not text:
        raise ConfigurationError(f"{field} must be a path")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _default_telos_root() -> Path:
    return (Path.home() / ".telos").expanduser().resolve()


def _clean_agent_id(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "default"
    safe = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in text)
    return safe.strip("._-") or "default"


def _coerce_non_negative_int(value: Any, *, field: str, default: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"{field} must be a non-negative integer") from error
    if parsed < 0:
        raise ConfigurationError(f"{field} must be a non-negative integer")
    return parsed


def _coerce_positive_int(value: Any, *, field: str, default: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"{field} must be a positive integer") from error
    if parsed <= 0:
        raise ConfigurationError(f"{field} must be a positive integer")
    return parsed


def _coerce_non_negative_float(value: Any, *, field: str, default: float) -> float:
    if value is None or str(value).strip() == "":
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"{field} must be a non-negative number") from error
    if parsed < 0:
        raise ConfigurationError(f"{field} must be a non-negative number")
    return parsed


def _coerce_positive_float(value: Any, *, field: str, default: float) -> float:
    if value is None or str(value).strip() == "":
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"{field} must be a positive number") from error
    if parsed <= 0:
        raise ConfigurationError(f"{field} must be a positive number")
    return parsed


def _resolve_braced_env(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 3 and text.startswith("{") and text.endswith("}"):
        env_name = text[1:-1].strip()
        if env_name:
            return os.getenv(env_name, "")
    return text


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigurationError(f"Expected object in config, got {type(value).__name__}")
    return value


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigurationError(f"Expected list in config, got {type(value).__name__}")
    return value


def write_config_mapping(path: Path, data: dict[str, Any]) -> None:
    """把配置映射写回磁盘（小而稳的 YAML 子集，与 `_parse_simple_yaml` 对应）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_config_mapping(data), encoding="utf-8")


def dump_config_mapping(data: dict[str, Any]) -> str:
    return _dump_yaml(data).rstrip() + "\n"


def _dump_yaml(value: Any, *, indent: int = 0) -> str:
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            prefix = " " * indent + _yaml_key(key) + ":"
            if _is_yaml_scalar(item):
                _append_yaml_scalar(lines, prefix, item, block_indent=indent + 2)
            elif isinstance(item, list) and not item:
                # 空列表须写成内联 [] —— 裸键会被解析器读回成空 dict {}，破坏往返。
                lines.append(prefix + " []")
            elif isinstance(item, dict) and not item:
                lines.append(prefix)
            else:
                lines.append(prefix)
                lines.append(_dump_yaml(item, indent=indent + 2))
        return "\n".join(line for line in lines if line != "")
    if isinstance(value, list):
        if not value:
            return " " * indent + "[]"
        lines = []
        for item in value:
            if _is_yaml_scalar(item):
                if isinstance(item, str) and "\n" in item:
                    lines.append(" " * indent + "- |")
                    lines.extend(" " * (indent + 2) + line for line in item.splitlines())
                else:
                    lines.append(" " * indent + "- " + _yaml_scalar(item))
            elif isinstance(item, dict) and item:
                first_key = next(iter(item))
                first_value = item[first_key]
                rest = {key: val for key, val in item.items() if key != first_key}
                if _is_yaml_scalar(first_value):
                    prefix = " " * indent + "- " + _yaml_key(first_key) + ":"
                    _append_yaml_scalar(lines, prefix, first_value, block_indent=indent + 2)
                else:
                    lines.append(" " * indent + "- " + _yaml_key(first_key) + ":")
                    lines.append(_dump_yaml(first_value, indent=indent + 4))
                if rest:
                    lines.append(_dump_yaml(rest, indent=indent + 2))
            else:
                lines.append(" " * indent + "- " + json.dumps(item, ensure_ascii=False))
        return "\n".join(lines)
    return " " * indent + _yaml_scalar(value)


def _is_yaml_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _append_yaml_scalar(
    lines: list[str],
    prefix: str,
    value: Any,
    *,
    block_indent: int,
) -> None:
    if isinstance(value, str) and "\n" in value:
        lines.append(prefix + " |")
        lines.extend(" " * block_indent + line for line in value.splitlines())
        return
    lines.append(prefix + " " + _yaml_scalar(value))


def _yaml_key(value: Any) -> str:
    text = str(value)
    if text and all(char.isalnum() or char in {"_", "-"} for char in text):
        return text
    return json.dumps(text, ensure_ascii=False)


def _yaml_scalar(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "":
        return '""'
    if _yaml_needs_quotes(text):
        return json.dumps(text, ensure_ascii=False)
    return text


def _yaml_needs_quotes(text: str) -> bool:
    if text != text.strip():
        return True
    if text in {"true", "false", "True", "False", "null", "None", "~"}:
        return True
    if text[0] in {"-", "?", ":", "{", "}", "[", "]", ",", "&", "*", "#", "!", "|", ">", "@", "`", '"', "'"}:
        return True
    if ": " in text or " #" in text:
        return True
    return False


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    rows = _yaml_rows(text)
    if not rows:
        return {}
    parsed, index = _parse_yaml_block(rows, 0, rows[0][0])
    if index != len(rows):
        raise ConfigurationError("Could not parse full YAML config")
    if not isinstance(parsed, dict):
        raise ConfigurationError("Top-level YAML config must be an object")
    return parsed


def _yaml_rows(text: str) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        rows.append((indent, raw_line.strip()))
    return rows


def _parse_yaml_block(
    rows: list[tuple[int, str]],
    index: int,
    indent: int,
) -> tuple[Any, int]:
    if rows[index][1].startswith("- "):
        result: list[Any] = []
        while index < len(rows):
            row_indent, content = rows[index]
            if row_indent < indent:
                break
            if row_indent != indent or not content.startswith("- "):
                break
            item = content[2:].strip()
            if item:
                value = _parse_yaml_list_item(item)
                index += 1
                if index < len(rows) and rows[index][0] > row_indent:
                    child, index = _parse_yaml_block(rows, index, rows[index][0])
                    if isinstance(value, dict) and isinstance(child, dict):
                        value.update(child)
                    else:
                        raise ConfigurationError(f"Invalid nested YAML list item: {item}")
                result.append(value)
            else:
                index += 1
                if index < len(rows) and rows[index][0] > row_indent:
                    child, index = _parse_yaml_block(rows, index, rows[index][0])
                    result.append(child)
                else:
                    result.append(None)
        return result, index

    result: dict[str, Any] = {}
    while index < len(rows):
        row_indent, content = rows[index]
        if row_indent < indent:
            break
        if row_indent != indent:
            break
        if ":" not in content:
            raise ConfigurationError(f"Invalid YAML line: {content}")
        key, raw_value = content.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if raw_value:
            if _is_yaml_block_scalar(raw_value):
                index += 1
                result[key], index = _parse_yaml_scalar_block(
                    rows,
                    index,
                    row_indent,
                    style=raw_value,
                )
            else:
                result[key] = _parse_scalar(raw_value)
                index += 1
        else:
            index += 1
            if index < len(rows) and (
                rows[index][0] > row_indent
                or (rows[index][0] == row_indent and rows[index][1].startswith("- "))
            ):
                child, index = _parse_yaml_block(rows, index, rows[index][0])
                result[key] = child
            else:
                result[key] = {}
    return result, index


def _is_yaml_block_scalar(value: str) -> bool:
    return value in {"|", "|-", "|+", ">", ">-", ">+"}


def _parse_yaml_scalar_block(
    rows: list[tuple[int, str]],
    index: int,
    parent_indent: int,
    *,
    style: str,
) -> tuple[str, int]:
    lines: list[str] = []
    while index < len(rows) and rows[index][0] > parent_indent:
        lines.append(rows[index][1])
        index += 1
    if style.startswith(">"):
        text = " ".join(line for line in lines if line)
    else:
        text = "\n".join(lines)
    if style.endswith("-"):
        return text, index
    return f"{text}\n" if text else "", index


def _parse_yaml_list_item(item: str) -> Any:
    if ":" not in item:
        return _parse_scalar(item)
    key, raw_value = item.split(":", 1)
    key = key.strip()
    if not key:
        return _parse_scalar(item)
    raw_value = raw_value.strip()
    return {key: _parse_scalar(raw_value) if raw_value else {}}


def _parse_scalar(value: str) -> Any:
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "None", "~"}:
        return None
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    if value.startswith(("[", "{")) and value.endswith(("]", "}")):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value
