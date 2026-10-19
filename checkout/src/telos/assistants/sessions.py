"""助手会话索引：按助手分文件记录每个用户的助手会话。

助手会话不能用 ``SessionManager.get_or_create`` —— 那会解析全局唯一的活跃会话，
与主会话冲突。该索引为每个助手维护一份按用户分键的会话列表，并用
``SessionManager.create(profile_name=..., activate=False)`` 创建会话，不抢占主会话的
活跃位置。

内置 ``things_coach`` 继续读写原路径 ``indexes/overlays/things_coach.json``、
conversation_key 前缀保持 ``overlay:things_coach|``（已有会话数据零迁移）；
自建助手索引用 ``indexes/assistants/<id>.json``，前缀用 ``assistant:<id>|``。
"""

from __future__ import annotations

import json
from pathlib import Path

from telos.adapters.base import ConversationRef
from telos.assistants.registry import THINGS_COACH_ASSISTANT_ID
from telos.core.clock import user_now_iso
from telos.session.manager import SessionManager
from telos.session.session import Session
from telos.storage.filesystem import ensure_dir, write_json_atomic


def user_key(ref: ConversationRef) -> str:
    tenant = ref.tenant_id or ref.app_id
    who = ref.user_id or ref.chat_id
    return f"{ref.platform}|{tenant}|{who}"


class AssistantSessionIndex:
    def __init__(
        self,
        path: Path,
        *,
        conversation_key_prefix: str,
        profile_name: str,
    ):
        self.path = Path(path).expanduser().resolve()
        self.conversation_key_prefix = conversation_key_prefix
        self.profile_name = profile_name
        ensure_dir(self.path.parent)

    @classmethod
    def for_assistant(cls, agent_dir: Path, assistant_id: str) -> AssistantSessionIndex:
        clean_id = str(assistant_id or "").strip()
        if clean_id == THINGS_COACH_ASSISTANT_ID:
            return cls(
                Path(agent_dir) / "indexes" / "overlays" / "things_coach.json",
                conversation_key_prefix=f"overlay:{THINGS_COACH_ASSISTANT_ID}|",
                profile_name=THINGS_COACH_ASSISTANT_ID,
            )
        return cls(
            Path(agent_dir) / "indexes" / "assistants" / f"{clean_id}.json",
            conversation_key_prefix=f"assistant:{clean_id}|",
            profile_name=clean_id,
        )

    def active_session(self, manager: SessionManager, ref: ConversationRef) -> Session:
        entry = self._load().get("users", {}).get(user_key(ref))
        if entry and entry.get("active_session_id"):
            existing = manager.load(str(entry["active_session_id"]))
            if existing is not None:
                return existing
        return self.new_session(manager, ref)

    def new_session(self, manager: SessionManager, ref: ConversationRef) -> Session:
        key = user_key(ref)
        data = self._load()
        users = data.setdefault("users", {})
        entry = users.setdefault(key, {"sessions": [], "active_session_id": ""})
        parent_session_id = str(entry.get("active_session_id") or "")
        session = manager.create(
            ref,
            conversation_key_value=f"{self.conversation_key_prefix}{key}",
            parent_session_id=parent_session_id,
            profile_name=self.profile_name,
            activate=False,
        )
        entry.setdefault("sessions", []).append(session.id)
        entry["active_session_id"] = session.id
        entry["updated_at"] = user_now_iso()
        self._save(data)
        return session

    def session_ids(self, ref: ConversationRef) -> list[str]:
        entry = self._load().get("users", {}).get(user_key(ref)) or {}
        return list(entry.get("sessions", []))

    def _load(self) -> dict:
        if not self.path.exists():
            return {"users": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"users": {}}
        return data if isinstance(data, dict) else {"users": {}}

    def _save(self, data: dict) -> None:
        write_json_atomic(self.path, data)
