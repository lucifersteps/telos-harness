"""knowledge.sqlite repository：活动投影。

投影是 agent 阅读活动图谱的唯一入口，由智能体目录下的 `runtime/activity.sqlite`
生成，写入方见 `telos.activity.projection`。只保留活动的当前状态，不含变更记录、
环路水位线和 capture 级归属明细。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from telos.storage.filesystem import ensure_dir


@dataclass(frozen=True)
class ActivityProjectionRow:
    id: str
    status: str
    title: str
    description: str
    span_start: str
    span_end: str
    active_seconds: float
    detail_ids: list[str] = field(default_factory=list)


class ActivityProjectionRepository:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()
        ensure_dir(self.path.parent)
        self._connection = sqlite3.connect(str(self.path))
        self._connection.row_factory = sqlite3.Row
        self._init_schema()

    @classmethod
    def for_memory_space(cls, root: Path) -> "ActivityProjectionRepository":
        return cls(Path(root) / "knowledge" / "knowledge.sqlite")

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "ActivityProjectionRepository":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.close()

    def upsert(self, rows: list[ActivityProjectionRow]) -> None:
        if not rows:
            return
        with self._connection:
            self._connection.executemany(
                """
                INSERT INTO activities (
                    id, status, title, description,
                    span_start, span_end, active_seconds, detail_ids
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    title = excluded.title,
                    description = excluded.description,
                    span_start = excluded.span_start,
                    span_end = excluded.span_end,
                    active_seconds = excluded.active_seconds,
                    detail_ids = excluded.detail_ids
                """,
                [
                    (
                        str(row.id),
                        str(row.status),
                        str(row.title),
                        str(row.description or ""),
                        str(row.span_start),
                        str(row.span_end),
                        float(row.active_seconds),
                        json.dumps([str(item) for item in row.detail_ids], ensure_ascii=False),
                    )
                    for row in rows
                ],
            )

    def delete(self, activity_ids: list[str]) -> None:
        clean = [str(item) for item in activity_ids if str(item or "").strip()]
        if not clean:
            return
        with self._connection:
            self._connection.executemany(
                "DELETE FROM activities WHERE id = ?",
                [(activity_id,) for activity_id in clean],
            )

    def get(self, activity_id: str) -> ActivityProjectionRow | None:
        row = self._connection.execute(
            "SELECT * FROM activities WHERE id = ?",
            (str(activity_id),),
        ).fetchone()
        return _row_from_sqlite(row) if row is not None else None

    def list_all(self) -> list[ActivityProjectionRow]:
        rows = self._connection.execute(
            "SELECT * FROM activities ORDER BY span_start ASC, id ASC"
        ).fetchall()
        return [_row_from_sqlite(row) for row in rows]

    def _init_schema(self) -> None:
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS activities (
                  id             TEXT PRIMARY KEY,
                  status         TEXT NOT NULL,
                  title          TEXT NOT NULL,
                  description    TEXT NOT NULL DEFAULT '',
                  span_start     TEXT NOT NULL,
                  span_end       TEXT NOT NULL,
                  active_seconds REAL NOT NULL DEFAULT 0,
                  detail_ids     TEXT NOT NULL DEFAULT '[]'
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_activities_span "
                "ON activities(span_start, span_end)"
            )


def _row_from_sqlite(row: sqlite3.Row) -> ActivityProjectionRow:
    try:
        detail_ids = json.loads(str(row["detail_ids"] or "[]"))
    except ValueError:
        detail_ids = []
    return ActivityProjectionRow(
        id=str(row["id"]),
        status=str(row["status"]),
        title=str(row["title"]),
        description=str(row["description"]),
        span_start=str(row["span_start"]),
        span_end=str(row["span_end"]),
        active_seconds=float(row["active_seconds"]),
        detail_ids=[str(item) for item in list(detail_ids or [])],
    )
