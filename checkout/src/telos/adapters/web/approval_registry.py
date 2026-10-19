"""连接无关的待审批登记处 + 基于它的 ApprovalChannel。

审批不再绑在某条 WS 连接上：`ToolPolicyHook` 把一条持久事件
`tool.call.approval_request`(带 `request_id`)发进 turn 的 live stream(因此进 attach
快照,任意端重连都能重放出待审批)。真正阻塞该 turn 的 Future 存在这个进程级 registry 里,
按 `request_id` 寻址；任意 WS 收到 `client_action(action="approval")` 都能 `resolve`。

于是:切 tab / 刷新 / 多窗口都不丢审批;连接断开只是脱离取景器,不会误拒审批。
审批的超时仍由 `request_tool_approval` 的 `wait_for` 负责。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from telos.adapters.base import ApprovalRequest, ApprovalResponse


@dataclass(frozen=True)
class PendingApprovalEntry:
    request_id: str
    turn_id: str
    tool_call_id: str
    tool_name: str
    action: str
    future: asyncio.Future[ApprovalResponse]


@dataclass(frozen=True)
class ApprovalGrant:
    action: str
    tool_name: str
    scope: str
    expires_at: str = ""


class PendingApprovalRegistry:
    """进程级待审批登记处，按 request_id 寻址，也能按 turn_id 批量取消。"""

    def __init__(self, grants_path: Path | None = None) -> None:
        self._pending: dict[str, PendingApprovalEntry] = {}
        self._grants_path = grants_path
        self._grants = self._load_grants()

    def register(self, request: ApprovalRequest) -> asyncio.Future[ApprovalResponse]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalResponse] = loop.create_future()
        grant = self._grant_for(request)
        if grant is not None:
            future.set_result(ApprovalResponse(decision="approve", scope=grant.scope))
            return future
        self._pending[request.request_id] = PendingApprovalEntry(
            request_id=request.request_id,
            turn_id=request.turn_id,
            tool_call_id=request.tool_call_id,
            tool_name=request.tool_name,
            action=str(request.action or "").strip(),
            future=future,
        )
        return future

    def resolve(
        self,
        request_id: str,
        decision: str,
        feedback: str = "",
        *,
        scope: str = "",
        modified_args: dict[str, Any] | None = None,
    ) -> bool:
        """兑现某条待审批。返回是否命中一个未完成的 Future。"""
        entry = self._pending.get(request_id)
        if entry is None or entry.future.done():
            return False
        raw_decision = str(decision or "").strip().lower()
        normalized_decision = _normalize_decision(raw_decision)
        normalized_scope = _normalize_scope(scope)
        if raw_decision in {"approved_for_session", "approve_for_session"}:
            normalized_scope = "session"
        if normalized_decision == "approve" and normalized_scope in {
            "session",
            "today",
            "week",
        }:
            self.add_grant(
                tool_name=entry.tool_name,
                action=entry.action,
                scope=normalized_scope,
                current_request_id=request_id,
            )
        entry.future.set_result(
            ApprovalResponse(
                decision=normalized_decision,
                feedback=feedback,
                scope=normalized_scope,
                modified_args=modified_args,
            )
        )
        self._pending.pop(request_id, None)
        return True

    def add_grant(
        self,
        *,
        tool_name: str,
        action: str,
        scope: str,
        current_request_id: str = "",
    ) -> bool:
        normalized_scope = _normalize_scope(scope)
        clean_tool_name = str(tool_name or "").strip()
        if not clean_tool_name or normalized_scope not in {
            "session",
            "today",
            "week",
        }:
            return False
        grant = ApprovalGrant(
            action=str(action or "").strip(),
            tool_name=clean_tool_name,
            scope=normalized_scope,
            expires_at=_grant_expiry(normalized_scope),
        )
        self._grants.append(grant)
        self._save_grants()
        self._resolve_matching_grants(grant, current_request_id=current_request_id)
        return True

    def cancel_by_turn(self, turn_id: str, feedback: str = "cancelled by stop") -> int:
        """把某个 turn 下仍在等待的审批全部以 cancelled 响应兑现。"""
        if not turn_id:
            return 0
        cancelled = 0
        for request_id, entry in list(self._pending.items()):
            if entry.turn_id != turn_id or entry.future.done():
                continue
            entry.future.set_result(
                ApprovalResponse(decision="cancelled", feedback=feedback)
            )
            self._pending.pop(request_id, None)
            cancelled += 1
        return cancelled

    def cancel_by_tool_call(
        self,
        tool_call_id: str,
        feedback: str = "cancelled by stop",
    ) -> int:
        """把某个 tool_call 下仍在等待的审批以 cancelled 响应兑现。"""
        if not tool_call_id:
            return 0
        cancelled = 0
        for request_id, entry in list(self._pending.items()):
            if entry.tool_call_id != tool_call_id or entry.future.done():
                continue
            entry.future.set_result(
                ApprovalResponse(decision="cancelled", feedback=feedback)
            )
            self._pending.pop(request_id, None)
            cancelled += 1
        return cancelled

    def cancel_by_request_id(
        self,
        request_id: str,
        feedback: str = "cancelled by stop",
    ) -> bool:
        """把某个审批请求以 cancelled 响应兑现。"""
        clean_request_id = str(request_id or "").strip()
        if not clean_request_id:
            return False
        entry = self._pending.get(clean_request_id)
        if entry is None or entry.future.done():
            return False
        entry.future.set_result(
            ApprovalResponse(decision="cancelled", feedback=feedback)
        )
        self._pending.pop(clean_request_id, None)
        return True

    def discard(self, request_id: str) -> None:
        self._pending.pop(request_id, None)

    def pending_ids(self) -> list[str]:
        return [
            request_id
            for request_id, entry in self._pending.items()
            if not entry.future.done()
        ]

    def pending_ids_for_turn(self, turn_id: str) -> list[str]:
        return [
            request_id
            for request_id, entry in self._pending.items()
            if entry.turn_id == turn_id and not entry.future.done()
        ]

    def pending_entries(self) -> list[PendingApprovalEntry]:
        return [entry for entry in self._pending.values() if not entry.future.done()]

    def _grant_for(
        self,
        request: ApprovalRequest,
    ) -> ApprovalGrant | None:
        self._prune_expired_grants()
        action = str(request.action or "").strip()
        tool_name = str(request.tool_name or "").strip()
        for grant in self._grants:
            if _grant_matches(grant, tool_name=tool_name, action=action):
                return grant
        return None

    def _resolve_matching_grants(
        self,
        grant: ApprovalGrant,
        *,
        current_request_id: str,
    ) -> None:
        for request_id, entry in list(self._pending.items()):
            if request_id == current_request_id or entry.future.done():
                continue
            if not _grant_matches(
                grant,
                tool_name=entry.tool_name,
                action=entry.action,
            ):
                continue
            entry.future.set_result(ApprovalResponse(decision="approve", scope=grant.scope))
            self._pending.pop(request_id, None)

    def _load_grants(self) -> list[ApprovalGrant]:
        if self._grants_path is None or not self._grants_path.exists():
            return []
        try:
            payload = json.loads(self._grants_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return []
        grants: list[ApprovalGrant] = []
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict):
                continue
            scope = _normalize_scope(item.get("scope", ""))
            if scope not in {"today", "week"}:
                continue
            grant = ApprovalGrant(
                action=str(item.get("action", "")).strip(),
                tool_name=str(item.get("tool_name", "")).strip(),
                scope=scope,
                expires_at=str(item.get("expires_at", "")).strip(),
            )
            if grant.tool_name and not _grant_expired(grant):
                grants.append(grant)
        return grants

    def _save_grants(self) -> None:
        if self._grants_path is None:
            return
        persistent = [
            grant
            for grant in self._grants
            if grant.scope in {"today", "week"} and not _grant_expired(grant)
        ]
        payload = [
            {
                "tool_name": grant.tool_name,
                "action": grant.action,
                "scope": grant.scope,
                "expires_at": grant.expires_at,
            }
            for grant in persistent
        ]
        self._grants_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._grants_path.with_suffix(f"{self._grants_path.suffix}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self._grants_path)

    def _prune_expired_grants(self) -> None:
        active = [grant for grant in self._grants if not _grant_expired(grant)]
        if len(active) == len(self._grants):
            return
        self._grants = active
        self._save_grants()


class RegistryApprovalChannel:
    """实现 `ApprovalChannel`：在 registry 注册 Future 并等待,不发任何 WS 帧。

    审批请求已由 `tool.call.approval_request` 事件经 live stream 送达所有 attach 的视图;
    这里只负责阻塞该 turn 直到 `registry.resolve(request_id, ...)` 或(由上层 `wait_for`)超时。
    单实例可跨所有 web turn 共享(main / 助手直连 / 助手委派)——按 `request_id` 路由。
    """

    def __init__(self, registry: PendingApprovalRegistry) -> None:
        self._registry = registry

    async def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        request_id = request.request_id
        if not request_id:
            return ApprovalResponse(
                decision="deny", feedback="missing approval request id"
            )
        future = self._registry.register(request)
        try:
            return await future
        finally:
            self._registry.discard(request_id)


def _normalize_decision(value: str) -> str:
    text = str(value or "").strip().lower()
    if text in {"approve", "approved", "yes", "1"}:
        return "approve"
    if text in {"approved_for_session", "approve_for_session"}:
        return "approve"
    if text in {"explain", "request_explanation", "explanation"}:
        return "explain"
    if text in {"cancel", "cancelled", "canceled"}:
        return "cancelled"
    return "deny" if text in {"deny", "denied", "reject", "rejected", "no", "0"} else text


def _normalize_scope(value: str) -> str:
    text = str(value or "").strip().lower()
    if text in {"session", "today", "week", "once", "worker"}:
        return text
    return ""


def _grant_matches(
    grant: ApprovalGrant,
    *,
    tool_name: str,
    action: str,
) -> bool:
    return grant.tool_name == tool_name and (not grant.action or grant.action == action)


def _grant_expiry(scope: str) -> str:
    now = datetime.now().astimezone()
    if scope == "today":
        expires = datetime.combine(now.date() + timedelta(days=1), time.min, now.tzinfo)
    elif scope == "week":
        expires = datetime.combine(
            now.date() + timedelta(days=7 - now.weekday()),
            time.min,
            now.tzinfo,
        )
    else:
        return ""
    return expires.isoformat()


def _grant_expired(grant: ApprovalGrant) -> bool:
    if not grant.expires_at:
        return False
    try:
        expires = datetime.fromisoformat(grant.expires_at)
    except ValueError:
        return True
    now = datetime.now(expires.tzinfo) if expires.tzinfo is not None else datetime.now()
    return expires <= now
