"""WeChat adapter state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from telos.adapters.base import ConversationRef
from telos.core.clock import parse_user_time, to_user_time, user_now_iso
from telos.storage.filesystem import read_json, write_json_atomic


@dataclass(frozen=True)
class WeChatAccount:
    account_id: str
    token: str
    base_url: str
    user_id: str = ""
    saved_at: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WeChatAccount":
        return cls(
            account_id=str(raw["account_id"]),
            token=str(raw.get("token") or ""),
            base_url=str(raw.get("base_url") or ""),
            user_id=str(raw.get("user_id") or ""),
            saved_at=str(raw.get("saved_at") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "token": self.token,
            "base_url": self.base_url,
            "user_id": self.user_id,
            "saved_at": self.saved_at or user_now_iso(),
        }


class WeChatStateStore:
    def __init__(self, directory: Path):
        self.directory = directory

    def account_path(self, account_id: str) -> Path:
        return self.directory / "accounts" / f"{account_id}.json"

    def context_tokens_path(self, account_id: str) -> Path:
        return self.directory / "accounts" / f"{account_id}.context-tokens.json"

    def pending_approvals_path(self, account_id: str = "") -> Path:
        account_id = str(account_id or "").strip()
        if account_id:
            return self.directory / "accounts" / f"{account_id}.approvals.json"
        return self.directory / "pending-approvals.json"

    def save_account(self, account: WeChatAccount) -> None:
        write_json_atomic(self.account_path(account.account_id), account.to_dict())

    def load_account(self, account_id: str) -> WeChatAccount | None:
        path = self.account_path(account_id)
        if not path.exists():
            return None
        data = read_json(path, {})
        if not isinstance(data, dict):
            return None
        return WeChatAccount.from_dict(data)

    def list_accounts(self) -> list[WeChatAccount]:
        accounts_dir = self.directory / "accounts"
        if not accounts_dir.exists():
            return []
        accounts: list[WeChatAccount] = []
        for path in sorted(accounts_dir.glob("*.json")):
            if _is_account_sidecar_path(path):
                continue
            data = read_json(path, {})
            if isinstance(data, dict):
                accounts.append(WeChatAccount.from_dict({**data, "account_id": data.get("account_id") or path.stem}))
        return accounts

    def load_default_account(self) -> WeChatAccount | None:
        accounts = self.list_accounts()
        if not accounts:
            return None
        return max(accounts, key=lambda account: account.saved_at)

    def load_context_tokens(self, account_id: str) -> dict[str, str]:
        data = read_json(self.context_tokens_path(account_id), {})
        if not isinstance(data, dict):
            return {}
        return {
            str(chat_id): str(token)
            for chat_id, token in data.items()
            if str(chat_id).strip() and str(token).strip()
        }

    def get_context_token(self, account_id: str, chat_id: str) -> str:
        return self.load_context_tokens(account_id).get(str(chat_id or ""), "")

    def set_context_token(self, account_id: str, chat_id: str, token: str) -> None:
        chat_id = str(chat_id or "").strip()
        token = str(token or "").strip()
        if not account_id or not chat_id:
            return
        tokens = self.load_context_tokens(account_id)
        if token:
            tokens[chat_id] = token
        else:
            tokens.pop(chat_id, None)
        write_json_atomic(self.context_tokens_path(account_id), tokens)

    def clear_context_token(self, account_id: str, chat_id: str) -> None:
        self.set_context_token(account_id, chat_id, "")

    def record_pending_approval(
        self,
        account_id: str,
        tool_call_id: str,
        ref: ConversationRef,
        *,
        created_at: datetime,
        expires_at: datetime | None,
    ) -> None:
        tool_call_id = str(tool_call_id or "").strip()
        if not tool_call_id:
            return
        path = self.pending_approvals_path(account_id)
        data = self._load_pending_approvals(account_id)
        data[tool_call_id] = {
            "platform": ref.platform,
            "tenant_id": ref.tenant_id,
            "app_id": ref.app_id,
            "chat_id": ref.chat_id,
            "thread_id": ref.thread_id,
            "user_id": ref.user_id,
            "source_id": ref.source_id,
            "created_at": _datetime_to_iso(created_at),
            "expires_at": _datetime_to_iso(expires_at) if expires_at is not None else "",
        }
        write_json_atomic(path, data)

    def clear_pending_approval(self, account_id: str, tool_call_id: str) -> None:
        tool_call_id = str(tool_call_id or "").strip()
        if not tool_call_id:
            return
        path = self.pending_approvals_path(account_id)
        data = self._load_pending_approvals(account_id)
        if tool_call_id not in data:
            return
        data.pop(tool_call_id, None)
        write_json_atomic(path, data)

    def has_pending_approval(
        self,
        account_id: str,
        ref: ConversationRef,
        *,
        now: datetime,
    ) -> bool:
        data = self._load_pending_approvals(account_id)
        if not data:
            return False
        normalized_now = to_user_time(now)
        changed = False
        found = False
        for tool_call_id, raw in list(data.items()):
            expires_at = _parse_datetime(str(raw.get("expires_at") or ""))
            if expires_at is not None and expires_at <= normalized_now:
                data.pop(tool_call_id, None)
                changed = True
                continue
            if _approval_ref_matches(raw, ref):
                found = True
        if changed:
            write_json_atomic(self.pending_approvals_path(account_id), data)
        return found

    def _load_pending_approvals(self, account_id: str) -> dict[str, dict[str, Any]]:
        data = read_json(self.pending_approvals_path(account_id), {})
        if not isinstance(data, dict):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for key, value in data.items():
            if str(key).strip() and isinstance(value, dict):
                result[str(key)] = value
        return result


def _approval_ref_matches(raw: dict[str, Any], ref: ConversationRef) -> bool:
    if str(raw.get("platform") or "") != ref.platform:
        return False
    if str(raw.get("chat_id") or "") != ref.chat_id:
        return False
    raw_source = str(raw.get("source_id") or "")
    if raw_source and ref.source_id and raw_source != ref.source_id:
        return False
    raw_thread = str(raw.get("thread_id") or "")
    if raw_thread and ref.thread_id and raw_thread != ref.thread_id:
        return False
    return True


def _is_account_sidecar_path(path: Path) -> bool:
    return path.name.endswith(
        (
            ".context-tokens.json",
            ".approvals.json",
        )
    )


def _datetime_to_iso(value: datetime) -> str:
    return to_user_time(value).isoformat(timespec="seconds")


def _parse_datetime(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    try:
        return parse_user_time(text)
    except ValueError:
        return None
