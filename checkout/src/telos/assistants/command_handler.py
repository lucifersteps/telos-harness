"""助手斜杠命令处理器，按助手维度接入会话命令路由器。"""

from __future__ import annotations

from telos.adapters.base import InboundTurn
from telos.assistants.sessions import AssistantSessionIndex
from telos.conversation.command_router import (
    CommandHelpEntry,
    ConversationCommandContext,
    ConversationCommandResult,
    routed_command_from_turn,
)
from telos.session.manager import SessionManager
from telos.session.session import Session


def new_session_response(label: str) -> str:
    return f"已开启新的{label}对话，对话上下文重新开始。"


def assistant_help_entries(label: str) -> list[CommandHelpEntry]:
    return [
        CommandHelpEntry("/new", f"开启新的{label}对话。"),
        CommandHelpEntry("/reload", "保留对话并重新加载记忆、提示词和工具。"),
        CommandHelpEntry("/help", f"查看{label}命令。"),
        CommandHelpEntry("/status", f"查看当前{label}会话状态。"),
    ]


def assistant_help_response(label: str) -> str:
    lines = [f"**{label}指令**"]
    for entry in assistant_help_entries(label):
        lines.append(f"- `{entry.command}`：{entry.description}")
    return "\n".join(lines)


class AssistantCommandHandler:
    def __init__(
        self,
        *,
        label: str,
        sessions: AssistantSessionIndex,
        session_manager: SessionManager,
    ):
        self.label = label
        self.sessions = sessions
        self.session_manager = session_manager

    def handle(
        self,
        turn: InboundTurn,
        session: Session | None,
        context: ConversationCommandContext | None = None,
    ) -> ConversationCommandResult | None:
        del session
        command = routed_command_from_turn(
            turn,
            registry=(context.command_registry if context is not None else None),
        )
        if command is None:
            return None
        if command.name in {"new", "reset"}:
            new_session = self.sessions.new_session(self.session_manager, turn.ref)
            return ConversationCommandResult(
                command="assistant:new",
                response=new_session_response(self.label),
                data={
                    "session_id": new_session.id,
                    "root_agent_id": new_session.metadata.root_agent_id,
                },
            )
        if command.name == "info":
            current = self.sessions.active_session(self.session_manager, turn.ref)
            count = len(self.sessions.session_ids(turn.ref))
            return ConversationCommandResult(
                command="assistant:status",
                response=f"当前{self.label}会话 {current.id}，历史会话共 {count} 个。",
                data={
                    "session_id": current.id,
                    "root_agent_id": current.metadata.root_agent_id,
                },
            )
        if command.name == "reload":
            current = self.sessions.active_session(self.session_manager, turn.ref)
            reloaded = self.session_manager.reload_session_context(current, turn.ref)
            return ConversationCommandResult(
                command="assistant:reload",
                response="已重新加载记忆、提示词和工具，当前对话已保留。",
                data={
                    "session_id": reloaded.id,
                    "root_agent_id": reloaded.metadata.root_agent_id,
                },
            )
        if command.name == "help":
            current = self.sessions.active_session(self.session_manager, turn.ref)
            return ConversationCommandResult(
                command="assistant:help",
                response=assistant_help_response(self.label),
                data={"session_id": current.id},
            )
        current = self.sessions.active_session(self.session_manager, turn.ref)
        return ConversationCommandResult(
            command=f"assistant:unsupported:{command.name}",
            response=f"这个指令在{self.label}页面不可用。",
            data={"session_id": current.id},
        )
