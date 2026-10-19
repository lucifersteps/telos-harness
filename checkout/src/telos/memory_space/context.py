"""context.sqlite repository."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telos.core.clock import user_now_iso
from telos.core.coerce import clean_ids
from telos.storage.filesystem import ensure_dir


@dataclass(frozen=True)
class ContextMessageRecord:
    seq: int
    session_id: str
    turn_id: str
    turn_index: int
    step_id: str
    step_index: int
    agent_id: str
    agent_name: str
    role: str
    content: Any
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str = ""
    tool_name: str = ""
    source_tool_call_id: str = ""
    created_at: str = ""


class ContextMessageRepository:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()
        ensure_dir(self.path.parent)
        self._connection = sqlite3.connect(str(self.path))
        self._connection.row_factory = sqlite3.Row
        self._init_schema()

    @classmethod
    def for_memory_space(cls, root: Path) -> "ContextMessageRepository":
        return cls(Path(root) / "context" / "context.sqlite")

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "ContextMessageRepository":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.close()

    def next_turn_index(self, session_id: str) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(turn_index), 0) AS value FROM context_messages WHERE session_id = ?",
            (str(session_id),),
        ).fetchone()
        return int(row["value"] or 0) + 1

    def append(
        self,
        *,
        session_id: str,
        turn_id: str,
        turn_index: int,
        step_id: str = "",
        step_index: int = 0,
        agent_id: str,
        agent_name: str,
        role: str,
        content: Any,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str = "",
        tool_name: str = "",
        source_tool_call_id: str = "",
        created_at: str = "",
    ) -> ContextMessageRecord:
        timestamp = created_at or user_now_iso()
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO context_messages (
                    session_id, turn_id, turn_index, step_id, step_index,
                    agent_id, agent_name, role, content, tool_calls,
                    tool_call_id, tool_name, source_tool_call_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(session_id),
                    str(turn_id),
                    int(turn_index),
                    str(step_id or ""),
                    int(step_index or 0),
                    str(agent_id),
                    str(agent_name),
                    str(role),
                    _json_dumps(content),
                    _json_dumps([dict(item) for item in list(tool_calls or [])]),
                    str(tool_call_id or ""),
                    str(tool_name or ""),
                    str(source_tool_call_id or ""),
                    timestamp,
                ),
            )
        row = self._connection.execute(
            "SELECT * FROM context_messages WHERE seq = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
        return _message_from_row(row)

    def list_messages(
        self,
        *,
        session_id: str = "",
        session_ids: list[str] | None = None,
        agent_id: str = "",
        seq_start: int = 0,
        seq_end: int = 0,
        exclude_turn_ids: list[str] | tuple[str, ...] | None = None,
    ) -> list[ContextMessageRecord]:
        sql = "SELECT * FROM context_messages WHERE 1 = 1"
        values: list[Any] = []
        if session_id:
            sql += " AND session_id = ?"
            values.append(str(session_id))
        elif session_ids is not None:
            ids = clean_ids(session_ids)
            if not ids:
                return []
            sql += f" AND session_id IN ({_placeholders(ids)})"
            values.extend(ids)
        if agent_id:
            sql += " AND agent_id = ?"
            values.append(str(agent_id))
        excluded = clean_ids(list(exclude_turn_ids or []))
        if excluded:
            sql += f" AND turn_id NOT IN ({_placeholders(excluded)})"
            values.extend(excluded)
        if seq_start:
            sql += " AND seq >= ?"
            values.append(int(seq_start))
        if seq_end:
            sql += " AND seq <= ?"
            values.append(int(seq_end))
        sql += " ORDER BY seq ASC"
        rows = self._connection.execute(sql, values).fetchall()
        return [_message_from_row(row) for row in rows]

    def list_messages_page(
        self,
        *,
        session_ids: list[str] | None = None,
        before_seq: int = 0,
        limit: int = 80,
    ) -> list[ContextMessageRecord]:
        page_size = max(1, int(limit or 80))
        sql = "SELECT * FROM context_messages WHERE 1 = 1"
        values: list[Any] = []
        if session_ids is not None:
            ids = clean_ids(session_ids)
            if not ids:
                return []
            sql += f" AND session_id IN ({_placeholders(ids)})"
            values.extend(ids)
        if before_seq:
            sql += " AND seq < ?"
            values.append(int(before_seq))
        sql += " ORDER BY seq DESC LIMIT ?"
        values.append(page_size)
        rows = self._connection.execute(sql, values).fetchall()
        return list(reversed([_message_from_row(row) for row in rows]))

    def list_tool_messages(
        self,
        tool_call_ids: list[str],
        *,
        session_ids: list[str] | None = None,
    ) -> list[ContextMessageRecord]:
        ids = clean_ids(tool_call_ids)
        if not ids:
            return []
        sql = (
            "SELECT * FROM context_messages "
            f"WHERE role = 'tool' AND tool_call_id IN ({_placeholders(ids)})"
        )
        values: list[Any] = list(ids)
        if session_ids is not None:
            scoped = clean_ids(session_ids)
            if not scoped:
                return []
            sql += f" AND session_id IN ({_placeholders(scoped)})"
            values.extend(scoped)
        sql += " ORDER BY seq ASC"
        rows = self._connection.execute(sql, values).fetchall()
        return [_message_from_row(row) for row in rows]

    def message_time_range(
        self,
        *,
        session_id: str,
        seq_start: int,
        seq_end: int,
    ) -> tuple[str, str]:
        values = (str(session_id), int(seq_start or 0), int(seq_end or 0))
        first = self._connection.execute(
            """
            SELECT created_at FROM context_messages
            WHERE session_id = ? AND seq >= ? AND seq <= ? AND created_at != ''
            ORDER BY seq ASC LIMIT 1
            """,
            values,
        ).fetchone()
        last = self._connection.execute(
            """
            SELECT created_at FROM context_messages
            WHERE session_id = ? AND seq >= ? AND seq <= ? AND created_at != ''
            ORDER BY seq DESC LIMIT 1
            """,
            values,
        ).fetchone()
        return (
            str(first["created_at"]) if first else "",
            str(last["created_at"]) if last else "",
        )

    def _init_schema(self) -> None:
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS context_messages (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,

                  session_id TEXT NOT NULL,

                  turn_id TEXT NOT NULL,
                  turn_index INTEGER NOT NULL,

                  step_id TEXT NOT NULL DEFAULT '',
                  step_index INTEGER NOT NULL DEFAULT 0,

                  agent_id TEXT NOT NULL,
                  agent_name TEXT NOT NULL,

                  role TEXT NOT NULL,
                  content TEXT NOT NULL,

                  tool_calls TEXT NOT NULL DEFAULT '[]',
                  tool_call_id TEXT NOT NULL DEFAULT '',
                  tool_name TEXT NOT NULL DEFAULT '',

                  source_tool_call_id TEXT NOT NULL DEFAULT '',

                  created_at TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_context_session_seq ON context_messages(session_id, seq)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_context_turn_seq ON context_messages(turn_id, seq)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_context_step_seq ON context_messages(step_id, seq)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_context_agent ON context_messages(agent_id, seq)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_context_tool_call ON context_messages(tool_call_id)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_context_source_tool_call ON context_messages(source_tool_call_id)"
            )


def _message_from_row(row: sqlite3.Row) -> ContextMessageRecord:
    return ContextMessageRecord(
        seq=int(row["seq"]),
        session_id=str(row["session_id"]),
        turn_id=str(row["turn_id"]),
        turn_index=int(row["turn_index"]),
        step_id=str(row["step_id"] or ""),
        step_index=int(row["step_index"] or 0),
        agent_id=str(row["agent_id"]),
        agent_name=str(row["agent_name"]),
        role=str(row["role"]),
        content=json.loads(str(row["content"])),
        tool_calls=[
            dict(item)
            for item in list(json.loads(str(row["tool_calls"] or "[]")) or [])
            if isinstance(item, dict)
        ],
        tool_call_id=str(row["tool_call_id"] or ""),
        tool_name=str(row["tool_name"] or ""),
        source_tool_call_id=str(row["source_tool_call_id"] or ""),
        created_at=str(row["created_at"]),
    )


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _placeholders(values: list[str]) -> str:
    return ",".join("?" for _item in values)
