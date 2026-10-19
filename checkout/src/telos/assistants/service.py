"""长期助手服务：把一条消息路由进指定助手的持续会话。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from telos.adapters.approval_channel import ApprovalChannel
from telos.adapters.base import ConversationRef, InboundTurn
from telos.adapters.question_channel import QuestionChannel
from telos.agent.hooks.policy.engine import PolicyEngine
from telos.agent.hooks.policy.tool_policy import ToolPolicyHook
from telos.agent.tools.manager import ToolManager
from telos.agent.turn_flow import run_text_turn
from telos.assistants.command_handler import AssistantCommandHandler
from telos.assistants.locks import AssistantLocks
from telos.assistants.registry import AssistantDefinition, AssistantRegistry
from telos.assistants.sessions import AssistantSessionIndex
from telos.conversation.command_router import ConversationCommandRouter
from telos.core.retry import RetryPolicy
from telos.providers.base import ChatProvider
from telos.runtime.cancel import CancelHandle
from telos.runtime.turn_runtime import RunSource, TurnRuntime
from telos.session.manager import SessionManager
from telos.tools.builtin.ask_user import create_ask_user_tool
from telos.tools.things.repository import ThingsRepository
from telos.tools.things.tools import create_things_tools


@dataclass(frozen=True)
class AssistantResult:
    status: str
    session_id: str
    turn_id: str
    response: str
    agent_name: str


class AssistantService:
    """长期助手入口：每个助手每个用户一个持续会话，两个消息来源（用户直连 / 主 agent 委派）。"""

    def __init__(
        self,
        *,
        session_manager: SessionManager,
        provider: ChatProvider,
        repo_root: Path,
        agent_dir: Path,
        registry: AssistantRegistry,
        max_steps: int = 20,
        retry_policy: RetryPolicy | None = None,
        turn_runtime: TurnRuntime | None = None,
    ):
        self.session_manager = session_manager
        self.provider = provider
        self.repo_root = Path(repo_root)
        self.agent_dir = Path(agent_dir)
        self.registry = registry
        self.max_steps = max_steps
        self.retry_policy = retry_policy or RetryPolicy()
        self.turn_runtime = turn_runtime
        self.locks = AssistantLocks()
        self._session_indexes: dict[str, AssistantSessionIndex] = {}
        self._command_routers: dict[str, ConversationCommandRouter] = {}

    def reload_registry(self, registry: AssistantRegistry) -> None:
        """配置变更后替换注册表；已建的会话索引与磁盘数据保留。"""
        self.registry = registry

    def sessions_for(self, assistant_id: str) -> AssistantSessionIndex:
        self.registry.get(assistant_id)
        index = self._session_indexes.get(assistant_id)
        if index is None:
            index = AssistantSessionIndex.for_assistant(self.agent_dir, assistant_id)
            self._session_indexes[assistant_id] = index
        return index

    async def handle_user_message(
        self,
        *,
        assistant_id: str,
        ref: ConversationRef,
        text: str,
        approval_channel: ApprovalChannel | None = None,
        question_channel: QuestionChannel | None = None,
        abort_check: CancelHandle | None = None,
    ) -> AssistantResult:
        definition = self.registry.get(assistant_id)
        router = self._command_router_for(definition)
        command = router.route(InboundTurn(ref=ref, text=text), None)
        if command is not None:
            return AssistantResult(
                status="command",
                session_id=str(command.data.get("session_id") or ""),
                turn_id=str(command.data.get("turn_id") or ""),
                response=command.response,
                agent_name=definition.id,
            )
        return await self._run(
            definition=definition,
            ref=ref,
            text=text,
            message_from="user",
            approval_channel=approval_channel,
            question_channel=question_channel,
            abort_check=abort_check,
        )

    async def handle_main_agent_message(
        self,
        *,
        assistant_id: str,
        user_ref: ConversationRef,
        message: str,
        approval_channel: ApprovalChannel | None = None,
        question_channel: QuestionChannel | None = None,
        abort_check: CancelHandle | None = None,
        source_tool_call_id: str = "",
        parent_turn_id: str = "",
    ) -> AssistantResult:
        return await self._run(
            definition=self.registry.get(assistant_id),
            ref=user_ref,
            text=message,
            message_from="main_agent",
            approval_channel=approval_channel,
            question_channel=question_channel,
            abort_check=abort_check,
            source_tool_call_id=source_tool_call_id,
            parent_turn_id=parent_turn_id,
        )

    async def _run(
        self,
        *,
        definition: AssistantDefinition,
        ref: ConversationRef,
        text: str,
        message_from: str,
        approval_channel: ApprovalChannel | None,
        question_channel: QuestionChannel | None,
        abort_check: CancelHandle | None,
        source_tool_call_id: str = "",
        parent_turn_id: str = "",
    ) -> AssistantResult:
        sessions = self.sessions_for(definition.id)
        session = sessions.active_session(self.session_manager, ref)
        async with self.locks.lock_for(session.id):
            tools = self._tools_for(definition, question_channel=question_channel)
            policy_hook = self._policy_hook_for(
                definition,
                approval_channel=approval_channel,
            )
            result = await run_text_turn(
                session=session,
                provider=self.provider,
                user_text=text,
                repo_root=self.repo_root,
                data_dir=self.agent_dir,
                profile_name=sessions.profile_name,
                profile_config=(definition.profile_config or None),
                profile_base_dir=definition.profile_base_dir,
                child_kind=definition.id,
                tools=tools,
                policy_hook=policy_hook,
                message_from=message_from,
                max_steps=self.max_steps,
                retry_policy=self.retry_policy,
                abort_check=abort_check,
                source_tool_call_id=source_tool_call_id,
                turn_runtime=self.turn_runtime,
                parent_turn_id=parent_turn_id,
                run_source=RunSource(
                    platform=ref.platform,
                    source_id=ref.source_id,
                    chat_id=ref.chat_id,
                    thread_id=ref.thread_id,
                    user_id=ref.user_id,
                ),
            )
        return AssistantResult(
            status="completed",
            session_id=session.id,
            turn_id=result.turn_id,
            response=result.response,
            agent_name=definition.id,
        )

    def _tools_for(
        self,
        definition: AssistantDefinition,
        *,
        question_channel: QuestionChannel | None,
    ) -> list:
        if definition.builtin:
            # 内置 things_coach 保持既有工具集：Things 工具族 + 可选 ask_user。
            tools = list(create_things_tools(ThingsRepository.for_agent_dir(self.agent_dir)))
            if question_channel is not None:
                tools = [*tools, create_ask_user_tool(question_channel)]
            return tools
        profile = self.registry.profile_for(definition)
        manager = ToolManager.with_builtins(
            list(profile.tools.enabled),
            data_dir=self.agent_dir,
            repo_root=self.repo_root,
            question_channel=question_channel,
        )
        return manager.list()

    def _policy_hook_for(
        self,
        definition: AssistantDefinition,
        *,
        approval_channel: ApprovalChannel | None,
    ) -> ToolPolicyHook:
        if definition.builtin:
            return ToolPolicyHook(
                engine=PolicyEngine(mode="custom"),
                approval_channel=approval_channel,
            )
        profile = self.registry.profile_for(definition)
        approvals = profile.policy.get("approvals")
        return ToolPolicyHook(
            engine=PolicyEngine(
                mode=str(profile.policy.get("mode") or "custom"),
                tool_approvals=approvals if isinstance(approvals, dict) else {},
            ),
            approval_channel=approval_channel,
        )

    def _command_router_for(
        self,
        definition: AssistantDefinition,
    ) -> ConversationCommandRouter:
        router = self._command_routers.get(definition.id)
        if router is None:
            router = ConversationCommandRouter(
                [
                    AssistantCommandHandler(
                        label=definition.label,
                        sessions=self.sessions_for(definition.id),
                        session_manager=self.session_manager,
                    )
                ]
            )
            self._command_routers[definition.id] = router
        return router
