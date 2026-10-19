"""recaps.sqlite repository."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from telos.core.clock import user_now_iso
from telos.core.coerce import clean_ids
from telos.core.ids import generate_id
from telos.storage.filesystem import ensure_dir

FAILED_CONTENT = "处理失败"


DEFAULT_AGENT_NAME = "main"


@dataclass(frozen=True)
class RecapRecord:
    recap_id: str
    session_id: str
    source_seq_start: int
    source_seq_end: int
    title: str
    content: str
    created_at: str
    agent_name: str = DEFAULT_AGENT_NAME


def recaps_covering_turn_ids(
    recaps: Iterable[RecapRecord],
    messages: Iterable[Any],
    turn_ids: Iterable[str],
) -> list[RecapRecord]:
    """找出来源范围包含指定轮次的 Recap。"""
    excluded = {str(turn_id or "").strip() for turn_id in turn_ids}
    excluded.discard("")
    if not excluded:
        return []
    affected_seqs = {
        int(getattr(message, "seq", 0) or 0)
        for message in messages
        if str(getattr(message, "turn_id", "") or "").strip() in excluded
    }
    affected_seqs.discard(0)
    if not affected_seqs:
        return []
    return [
        recap
        for recap in recaps
        if any(
            int(recap.source_seq_start or 0) <= seq <= int(recap.source_seq_end or 0)
            for seq in affected_seqs
        )
    ]


class RecapRepository:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()
        ensure_dir(self.path.parent)
        self._connection = sqlite3.connect(str(self.path))
        self._connection.row_factory = sqlite3.Row
        self._init_schema()

    @classmethod
    def for_memory_space(cls, root: Path) -> "RecapRepository":
        return cls(Path(root) / "recaps" / "recaps.sqlite")

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "RecapRepository":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.close()

    def append(
        self,
        *,
        session_id: str,
        source_seq_start: int,
        source_seq_end: int,
        title: str,
        content: str,
        recap_id: str = "",
        created_at: str = "",
        agent_name: str = DEFAULT_AGENT_NAME,
    ) -> RecapRecord:
        clean_title = str(title or "").strip()
        clean_content = str(content or "").strip()
        if not clean_title:
            raise ValueError("recap title is required")
        if not clean_content:
            raise ValueError("recap content is required")
        record = RecapRecord(
            recap_id=recap_id or generate_id("recap"),
            session_id=str(session_id),
            source_seq_start=int(source_seq_start or 0),
            source_seq_end=int(source_seq_end or 0),
            title=clean_title,
            content=clean_content,
            created_at=created_at or user_now_iso(),
            agent_name=str(agent_name or DEFAULT_AGENT_NAME),
        )
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO recaps (
                    recap_id, session_id, agent_name, source_seq_start, source_seq_end,
                    title, content, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.recap_id,
                    record.session_id,
                    record.agent_name,
                    record.source_seq_start,
                    record.source_seq_end,
                    record.title,
                    record.content,
                    record.created_at,
                ),
            )
        return record

    def append_failure(
        self,
        *,
        session_id: str,
        source_seq_start: int,
        source_seq_end: int,
        recap_id: str = "",
        agent_name: str = DEFAULT_AGENT_NAME,
    ) -> RecapRecord:
        return self.append(
            session_id=session_id,
            source_seq_start=source_seq_start,
            source_seq_end=source_seq_end,
            title=FAILED_CONTENT,
            content=FAILED_CONTENT,
            recap_id=recap_id,
            agent_name=agent_name,
        )

    def get(self, recap_id: str) -> RecapRecord | None:
        row = self._connection.execute(
            "SELECT * FROM recaps WHERE recap_id = ?", (str(recap_id),)
        ).fetchone()
        return _recap_from_row(row) if row is not None else None

    def delete(self, recap_id: str) -> bool:
        """删除一条 recap（用于失败重试：删掉占位后让 idle recap 重跑该区间）。"""
        cursor = self._connection.execute(
            "DELETE FROM recaps WHERE recap_id = ?", (str(recap_id),)
        )
        self._connection.commit()
        return cursor.rowcount > 0

    def list(
        self,
        *,
        session_id: str = "",
        session_ids: list[str] | None = None,
        agent_name: str = "",
        seq_start: int = 0,
        seq_end: int = 0,
    ) -> list[RecapRecord]:
        sql = "SELECT * FROM recaps"
        clauses: list[str] = []
        values: list[object] = []
        if session_id:
            clauses.append("session_id = ?")
            values.append(str(session_id))
        elif session_ids is not None:
            ids = clean_ids(session_ids)
            if not ids:
                return []
            clauses.append(f"session_id IN ({_placeholders(ids)})")
            values.extend(ids)
        if agent_name:
            clauses.append("agent_name = ?")
            values.append(str(agent_name))
        if seq_start:
            clauses.append("source_seq_end >= ?")
            values.append(int(seq_start))
        if seq_end:
            clauses.append("source_seq_end <= ?")
            values.append(int(seq_end))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC, source_seq_end ASC, recap_id ASC"
        rows = self._connection.execute(sql, values).fetchall()
        return [_recap_from_row(row) for row in rows]

    def _init_schema(self) -> None:
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recaps (
                  recap_id TEXT PRIMARY KEY,

                  session_id TEXT NOT NULL,
                  agent_name TEXT NOT NULL,
                  source_seq_start INTEGER NOT NULL DEFAULT 0,
                  source_seq_end INTEGER NOT NULL DEFAULT 0,

                  title TEXT NOT NULL,
                  content TEXT NOT NULL,

                  created_at TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_recaps_session_range ON recaps(session_id, source_seq_end)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_recaps_agent_session_range "
                "ON recaps(agent_name, session_id, source_seq_end)"
            )


def recap_run_context_uri(recap_id: str) -> str:
    clean = str(recap_id or "").strip()
    if not clean:
        raise ValueError("recap_id is required")
    return f"memory_space://recaps/runs/{clean}/context.jsonl"


def recap_run_json_uri(recap_id: str) -> str:
    clean = str(recap_id or "").strip()
    if not clean:
        raise ValueError("recap_id is required")
    return f"memory_space://recaps/runs/{clean}/run.json"


def _recap_from_row(row: sqlite3.Row) -> RecapRecord:
    return RecapRecord(
        recap_id=str(row["recap_id"]),
        session_id=str(row["session_id"]),
        source_seq_start=int(row["source_seq_start"] or 0),
        source_seq_end=int(row["source_seq_end"] or 0),
        title=str(row["title"]),
        content=str(row["content"]),
        created_at=str(row["created_at"]),
        agent_name=str(row["agent_name"]),
    )


def _placeholders(values: list[str]) -> str:
    return ",".join("?" for _item in values)
