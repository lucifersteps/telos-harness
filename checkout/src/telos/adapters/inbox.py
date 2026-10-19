"""Durable adapter inbox ledger backed by JSONL records."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from telos.adapters.base import AttachmentRef, ConversationRef, InboundTurn
from telos.core.clock import parse_user_time, user_now, user_now_iso, user_time_iso
from telos.core.coerce import safe_int
from telos.core.ids import generate_id
from telos.memory_space.runtime import validate_runtime_wire_event_type
from telos.storage.locks import file_lock

ADAPTER_LEDGER_SESSION_ID = "__adapter__"
DEFAULT_INBOX_LEASE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class AdapterInboxItem:
    dedupe_key: str
    adapter: str
    account_id: str
    raw_event_id: str
    source_id: str
    received_record_id: str
    received_at: str
    turn: InboundTurn
    raw: dict[str, Any]
    attempts: int = 0
    claim_id: str = ""
    lease_owner: str = ""
    lease_expires_at: str = ""
    completed: bool = False

    def claimed_turn(self) -> InboundTurn:
        metadata = dict(self.turn.metadata)
        keys = [
            str(item)
            for item in list(metadata.get("adapter_inbox_keys") or [])
            if str(item).strip()
        ]
        if self.dedupe_key not in keys:
            keys.append(self.dedupe_key)
        claims = dict(metadata.get("adapter_inbox_claims") or {})
        if self.claim_id:
            claims[self.dedupe_key] = self.claim_id
        return replace(
            self.turn,
            metadata={
                **metadata,
                "adapter_inbox_keys": keys,
                "adapter_inbox_claims": claims,
            },
        )


def _merge_claimed_items(items: list[AdapterInboxItem]) -> AdapterInboxItem:
    if len(items) == 1:
        return items[0]
    turns = [item.claimed_turn() for item in items]
    turn = replace(
        turns[-1],
        text="\n".join(turn.text for turn in turns if turn.text.strip()),
        attachments=[attachment for turn in turns for attachment in turn.attachments],
        metadata={
            **turns[-1].metadata,
            "adapter_inbox_keys": [
                key for turn in turns for key in turn.metadata["adapter_inbox_keys"]
            ],
            "adapter_inbox_claims": {
                key: value for turn in turns
                for key, value in turn.metadata["adapter_inbox_claims"].items()
            },
        },
    )
    return replace(items[-1], turn=turn)


class AdapterInboxLedger:
    def __init__(self, path: Path, *, agent_id: str):
        self.path = Path(path)
        self.agent_id = str(agent_id or "default").strip() or "default"

    def record_received_turn(
        self,
        *,
        adapter: str,
        account_id: str,
        turn: InboundTurn,
        raw: dict[str, Any] | None = None,
    ) -> AdapterInboxItem:
        adapter = _clean(adapter)
        account_id = _clean(account_id) or _clean(turn.ref.tenant_id) or "default"
        raw_event_id = _clean(turn.raw_event_id)
        source_id = _turn_source_id(turn)
        raw_data = dict(raw or {})
        dedupe_key = _dedupe_key(
            adapter=adapter,
            account_id=account_id,
            raw_event_id=raw_event_id,
            source_id=source_id,
            turn=turn,
            raw=raw_data,
        )
        with file_lock(self.path):
            state = self._reduce_unlocked()
            existing = state.items.get(dedupe_key)
            if existing is not None:
                return existing
            record = self._record(
                "adapter.inbound.received",
                data={
                    "dedupe_key": dedupe_key,
                    "adapter": adapter,
                    "account_id": account_id,
                    "raw_event_id": raw_event_id,
                    "source_id": source_id,
                    "turn": _turn_to_dict(turn),
                    "raw": raw_data,
                },
            )
            self._append_unlocked(record)
            return AdapterInboxItem(
                dedupe_key=dedupe_key,
                adapter=adapter,
                account_id=account_id,
                raw_event_id=raw_event_id,
                source_id=source_id,
                received_record_id=str(record["id"]),
                received_at=str(record["timestamp"]),
                turn=turn,
                raw=raw_data,
            )

    def claim_available(
        self,
        *,
        adapter: str,
        owner: str,
        limit: int = 100,
        lease_seconds: int = DEFAULT_INBOX_LEASE_SECONDS,
        group_available: Callable[
            [list[AdapterInboxItem]], list[list[AdapterInboxItem]]
        ] | None = None,
    ) -> list[AdapterInboxItem]:
        """按适配器的分组规则领取；暂未就绪的消息仍保留在持久化账本。"""
        adapter = _clean(adapter)
        owner = _clean(owner) or generate_id("daemon")
        now = user_now()
        lease_expires_at = (now + timedelta(seconds=max(lease_seconds, 1))).isoformat(timespec="seconds")
        claimed: list[AdapterInboxItem] = []
        with file_lock(self.path):
            state = self._reduce_unlocked()
            available = [
                item for item in state.ordered_items
                if item.adapter == adapter and not item.completed
                and (not item.claim_id or _lease_expired(item.lease_expires_at, now))
            ]
            groups = group_available(available) if group_available else [[item] for item in available]
            selected = [item.dedupe_key for group in groups for item in group]
            if len(selected) != len(set(selected)) or not set(selected) <= {
                item.dedupe_key for item in available
            }:
                raise ValueError("inbox groups must select each available message at most once")
            for group in groups[:limit]:
                members: list[AdapterInboxItem] = []
                for item in group:
                    claim_id = generate_id("claim")
                    next_attempt = int(item.attempts or 0) + 1
                    self._append_unlocked(self._record(
                        "adapter.inbound.claimed",
                        data={
                            "dedupe_key": item.dedupe_key,
                            "claim_id": claim_id,
                            "lease_owner": owner,
                            "lease_expires_at": lease_expires_at,
                            "attempt": next_attempt,
                        },
                    ))
                    members.append(replace(
                        item, attempts=next_attempt, claim_id=claim_id,
                        lease_owner=owner, lease_expires_at=lease_expires_at,
                    ))
                if members:
                    claimed.append(_merge_claimed_items(members))
        return claimed

    def complete(
        self,
        *,
        dedupe_key: str,
        claim_id: str = "",
        turn_id: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        key = _clean(dedupe_key)
        if not key:
            return
        with file_lock(self.path):
            state = self._reduce_unlocked()
            item = state.items.get(key)
            if item is None or item.completed:
                return
            self._append_unlocked(
                self._record(
                    "adapter.inbound.completed",
                    turn_id=turn_id,
                    data={
                        "dedupe_key": key,
                        "claim_id": _clean(claim_id),
                        "turn_id": _clean(turn_id),
                        "details": dict(details or {}),
                    },
                )
            )

    def release_incomplete_claims(self, *, reason: str) -> int:
        released = 0
        with file_lock(self.path):
            state = self._reduce_unlocked()
            for item in state.ordered_items:
                if item.completed or not item.claim_id:
                    continue
                self._append_unlocked(
                    self._record(
                        "adapter.inbound.released",
                        data={
                            "dedupe_key": item.dedupe_key,
                            "claim_id": item.claim_id,
                            "reason": _clean(reason),
                        },
                    )
                )
                released += 1
        return released

    def pending_count(self, *, adapter: str = "") -> int:
        adapter = _clean(adapter)
        state = self._reduce()
        count = 0
        for item in state.ordered_items:
            if adapter and item.adapter != adapter:
                continue
            if not item.completed:
                count += 1
        return count

    def record_cursor_committed(
        self,
        *,
        adapter: str,
        account_id: str,
        cursor_name: str,
        value: str,
    ) -> None:
        adapter = _clean(adapter)
        account_id = _clean(account_id) or "default"
        cursor_name = _clean(cursor_name) or "default"
        value = str(value or "")
        if not value:
            return
        current = self.latest_cursor(
            adapter=adapter,
            account_id=account_id,
            cursor_name=cursor_name,
        )
        if current == value:
            return
        with file_lock(self.path):
            self._append_unlocked(
                self._record(
                    "adapter.cursor.committed",
                    data={
                        "adapter": adapter,
                        "account_id": account_id,
                        "cursor_name": cursor_name,
                        "value": value,
                    },
                )
            )

    def latest_cursor(self, *, adapter: str, account_id: str, cursor_name: str) -> str:
        adapter = _clean(adapter)
        account_id = _clean(account_id) or "default"
        cursor_name = _clean(cursor_name) or "default"
        latest = ""
        for record in self._read_records():
            if record.get("type") != "adapter.cursor.committed":
                continue
            data = dict(record.get("data") or {})
            if (
                _clean(data.get("adapter")) == adapter
                and (_clean(data.get("account_id")) or "default") == account_id
                and (_clean(data.get("cursor_name")) or "default") == cursor_name
            ):
                latest = str(data.get("value") or "")
        return latest

    def _reduce(self) -> "_LedgerState":
        with file_lock(self.path):
            return self._reduce_unlocked()

    def _reduce_unlocked(self) -> "_LedgerState":
        items: dict[str, AdapterInboxItem] = {}
        order: list[str] = []
        for record in self._read_records_unlocked():
            record_type = str(record.get("type") or "")
            data = dict(record.get("data") or {})
            key = _clean(data.get("dedupe_key"))
            if record_type == "adapter.inbound.received":
                if not key or key in items:
                    continue
                turn_data = data.get("turn")
                if not isinstance(turn_data, dict):
                    continue
                item = AdapterInboxItem(
                    dedupe_key=key,
                    adapter=_clean(data.get("adapter")),
                    account_id=_clean(data.get("account_id")) or "default",
                    raw_event_id=_clean(data.get("raw_event_id")),
                    source_id=_clean(data.get("source_id")),
                    received_record_id=str(record.get("id") or ""),
                    received_at=str(record.get("timestamp") or ""),
                    turn=_turn_from_dict(turn_data),
                    raw=dict(data.get("raw") or {}),
                )
                items[key] = item
                order.append(key)
            elif record_type == "adapter.inbound.claimed":
                item = items.get(key)
                if item is None or item.completed:
                    continue
                items[key] = replace(
                    item,
                    attempts=max(item.attempts, safe_int(data.get("attempt"))),
                    claim_id=_clean(data.get("claim_id")),
                    lease_owner=_clean(data.get("lease_owner")),
                    lease_expires_at=str(data.get("lease_expires_at") or ""),
                )
            elif record_type == "adapter.inbound.released":
                item = items.get(key)
                if item is None or item.completed:
                    continue
                claim_id = _clean(data.get("claim_id"))
                if claim_id and item.claim_id and claim_id != item.claim_id:
                    continue
                items[key] = replace(
                    item,
                    claim_id="",
                    lease_owner="",
                    lease_expires_at="",
                )
            elif record_type == "adapter.inbound.completed":
                item = items.get(key)
                if item is None:
                    continue
                items[key] = replace(item, completed=True)
        return _LedgerState(items=items, ordered_keys=order)

    def _record(
        self,
        record_type: str,
        *,
        data: dict[str, Any],
        turn_id: str = "",
    ) -> dict[str, Any]:
        clean_type = validate_runtime_wire_event_type(record_type)
        return {
            "id": generate_id("rec"),
            "type": clean_type,
            "timestamp": user_now_iso(),
            "session_id": ADAPTER_LEDGER_SESSION_ID,
            "agent_id": self.agent_id,
            "turn_id": str(turn_id or ""),
            "data": data,
        }

    def _read_records(self) -> list[dict[str, Any]]:
        with file_lock(self.path):
            return self._read_records_unlocked()

    def _read_records_unlocked(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as file:
            for line_number, line in enumerate(file, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError as error:
                    raise ValueError(f"Invalid adapter ledger at {self.path}:{line_number}: {error}") from error
                if not isinstance(record, dict):
                    raise ValueError(f"Adapter ledger item at {self.path}:{line_number} is not an object")
                validate_runtime_wire_event_type(str(record.get("type") or ""))
                records.append(record)
        return records

    def _append_unlocked(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())


@dataclass(frozen=True)
class _LedgerState:
    items: dict[str, AdapterInboxItem]
    ordered_keys: list[str]

    @property
    def ordered_items(self) -> list[AdapterInboxItem]:
        return [self.items[key] for key in self.ordered_keys if key in self.items]


def _turn_source_id(turn: InboundTurn) -> str:
    ref = turn.ref
    if ref.source_id:
        return str(ref.source_id)
    parts = [ref.platform, ref.tenant_id, ref.chat_id, ref.thread_id, ref.user_id]
    return ":".join(str(part) for part in parts if str(part or "").strip())


def _dedupe_key(
    *,
    adapter: str,
    account_id: str,
    raw_event_id: str,
    source_id: str,
    turn: InboundTurn,
    raw: dict[str, Any],
) -> str:
    if raw_event_id:
        return f"{adapter}:{account_id}:{raw_event_id}"
    payload = {
        "source_id": source_id,
        "turn": _turn_to_dict(turn),
        "raw": raw,
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"{adapter}:{account_id}:sha256:{digest}"


def _turn_to_dict(turn: InboundTurn) -> dict[str, Any]:
    data = asdict(turn)
    timestamp = turn.timestamp
    data["timestamp"] = user_time_iso(timestamp) if timestamp is not None else ""
    return data


def _turn_from_dict(data: dict[str, Any]) -> InboundTurn:
    ref_data = dict(data.get("ref") or {})
    attachments = [
        AttachmentRef(
            kind=str(item.get("kind") or ""),
            uri=str(item.get("uri") or ""),
            name=str(item.get("name") or ""),
            mime_type=str(item.get("mime_type") or ""),
        )
        for item in list(data.get("attachments") or [])
        if isinstance(item, dict)
    ]
    return InboundTurn(
        ref=ConversationRef(
            platform=str(ref_data.get("platform") or ""),
            tenant_id=str(ref_data.get("tenant_id") or ""),
            app_id=str(ref_data.get("app_id") or ""),
            chat_id=str(ref_data.get("chat_id") or ""),
            thread_id=str(ref_data.get("thread_id") or ""),
            user_id=str(ref_data.get("user_id") or ""),
            source_id=str(ref_data.get("source_id") or ""),
        ),
        text=str(data.get("text") or ""),
        attachments=attachments,
        raw_event_id=str(data.get("raw_event_id") or ""),
        is_group=bool(data.get("is_group")),
        mentions_bot=bool(data.get("mentions_bot")),
        timestamp=_parse_datetime(str(data.get("timestamp") or "")),
        reply_to=str(data.get("reply_to") or ""),
        metadata=dict(data.get("metadata") or {}),
    )


def _parse_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return parse_user_time(text)
    except ValueError:
        return None


def _lease_expired(value: str, now: datetime) -> bool:
    expires_at = _parse_datetime(value)
    if expires_at is None:
        return True
    return expires_at <= now


def _clean(value: object) -> str:
    return str(value or "").strip()
