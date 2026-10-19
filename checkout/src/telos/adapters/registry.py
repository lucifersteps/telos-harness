"""Adapter registry."""

from __future__ import annotations

from pathlib import Path

from telos.adapters.approval_channel import SharedApprovalRegistry
from telos.adapters.base import Adapter
from telos.adapters.question_channel import SharedQuestionRegistry
from telos.adapters.cli.adapter import CliAdapter
from telos.adapters.lark.adapter import LarkAdapter
from telos.adapters.scheduler.adapter import SchedulerAdapter
from telos.adapters.wechat.adapter import WeChatAdapter
from telos.adapters.wechat.config import WeChatConfig
from telos.adapters.wechat.media import create_wechat_media_resolver
from telos.adapters.wechat.state import WeChatStateStore
from telos.adapters.web.adapter import WebAdapter
from telos.core.errors import ConfigurationError
from telos.memory_space.paths import AgentRuntimeLayout


def create_adapter(
    name: str,
    *,
    agent_dir: Path | None = None,
    approval_registry: SharedApprovalRegistry | None = None,
    question_registry: SharedQuestionRegistry | None = None,
) -> Adapter:
    adapter_name = str(name or "").strip().lower()
    if adapter_name == "web":
        return WebAdapter()
    if adapter_name == "cli":
        return CliAdapter()
    if adapter_name == "wechat":
        config = WeChatConfig.from_env()
        layout = AgentRuntimeLayout.for_agent_dir(agent_dir) if agent_dir else None
        state_store = WeChatStateStore(layout.runtime_wechat_dir) if layout else None
        media_resolver = None
        if layout:
            media_resolver = create_wechat_media_resolver(
                config,
                media_dir=layout.runtime_wechat_cache_dir,
            )
        return WeChatAdapter(
            config=config,
            state_store=state_store,
            media_resolver=media_resolver,
            approval_registry=approval_registry,
            question_registry=question_registry,
        )
    if adapter_name == "lark":
        return LarkAdapter(
            approval_registry=approval_registry,
            question_registry=question_registry,
        )
    if adapter_name == "scheduler":
        return SchedulerAdapter()
    raise ConfigurationError(f"Unknown adapter: {name!r}")


def create_adapters(
    names: list[str],
    *,
    agent_dir: Path | None = None,
    approval_registry: SharedApprovalRegistry | None = None,
    question_registry: SharedQuestionRegistry | None = None,
) -> list[Adapter]:
    return [
        create_adapter(
            name,
            agent_dir=agent_dir,
            approval_registry=approval_registry,
            question_registry=question_registry,
        )
        for name in names
    ]
