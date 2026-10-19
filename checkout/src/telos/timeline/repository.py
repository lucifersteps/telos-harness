"""Runtime SQLite repository for desktop timeline collection state."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from telos.core.clock import user_now, user_now_iso
from telos.timeline.grouping import group_timeline_sessions, timeline_session_group

DEFAULT_TIMELINE_DB_NAME = "collector.sqlite"


@dataclass(frozen=True)
class TimelineCapture:
    seq: int
    id: str
    timestamp: str
    trigger: str
    app_name: str = ""
    bundle_id: str = ""
    pid: int = 0
    window_title: str = ""
    window_key: str = ""
    url: str = ""
    document: str = ""
    screenshot_path: str = ""
    screenshot_sha256: str = ""
    image_digest: str = ""
    ax_text: str = ""
    ax_digest: str = ""
    text_source: str = ""
    needs_vision: bool = False
    skipped: bool = False
    need_duplicate: bool = False
    error: str = ""
    session_id: str = ""
    summary_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TimelineSession:
    seq: int
    id: str
    app_name: str
    bundle_id: str
    window_title: str
    window_key: str
    started_at: str
    ended_at: str
    duration_seconds: float
    capture_ids: list[str] = field(default_factory=list)
    non_duplicate_capture_ids: list[str] = field(default_factory=list)
    status: str = ""
    summary_id: str = ""
    created_at: str = ""
    updated_at: str = ""


class TimelineRepository:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path))
        self._connection.row_factory = sqlite3.Row
        self._init_schema()

    @classmethod
    def for_agent_dir(cls, agent_dir: Path) -> "TimelineRepository":
        return cls(agent_dir / "runtime" / "timeline" / DEFAULT_TIMELINE_DB_NAME)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "TimelineRepository":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.close()

    def append_capture(self, payload: dict[str, Any]) -> TimelineCapture:
        capture_id = str(payload.get("id") or f"tlcap-{uuid.uuid4().hex}")
        timestamp = str(payload.get("timestamp") or user_now_iso())
        app = payload.get("app") if isinstance(payload.get("app"), dict) else {}
        window = payload.get("window") if isinstance(payload.get("window"), dict) else {}
        row = (
            capture_id,
            timestamp,
            str(payload.get("trigger") or ""),
            str(app.get("name") or payload.get("app_name") or ""),
            str(app.get("bundle_id") or payload.get("bundle_id") or ""),
            int(app.get("pid") or payload.get("pid") or 0),
            str(window.get("title") or payload.get("window_title") or ""),
            str(payload.get("window_key") or ""),
            str(payload.get("url") or window.get("url") or ""),
            str(payload.get("document") or window.get("document") or ""),
            str(payload.get("screenshot_path") or ""),
            str(payload.get("screenshot_sha256") or ""),
            str(payload.get("image_digest") or ""),
            str(payload.get("ax_text") or ""),
            str(payload.get("ax_digest") or ""),
            str(payload.get("text_source") or ""),
            1 if bool(payload.get("needs_vision")) else 0,
            1 if bool(payload.get("skipped")) else 0,
            1 if bool(payload.get("need_duplicate")) else 0,
            _json_string(payload.get("error")),
            str(payload.get("session_id") or ""),
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
        )
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO timeline_captures (
                    id, timestamp, trigger, app_name, bundle_id, pid, window_title,
                    window_key, url, document, screenshot_path, screenshot_sha256,
                    image_digest, ax_text, ax_digest, text_source, needs_vision,
                    skipped, need_duplicate, error, session_id, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
        capture = self.get_capture(capture_id)
        if capture is None:  # pragma: no cover - sqlite just accepted it.
            raise RuntimeError("timeline capture insert did not round-trip")
        return capture

    def get_capture(self, capture_id: str) -> TimelineCapture | None:
        row = self._connection.execute(
            "SELECT * FROM timeline_captures WHERE id = ?",
            (str(capture_id),),
        ).fetchone()
        return _capture_from_row(row) if row is not None else None

    def list_pending_captures(self, *, limit: int = 0) -> list[TimelineCapture]:
        sql = """
            SELECT * FROM timeline_captures
            WHERE summary_id = ''
              AND skipped = 0
              AND need_duplicate = 0
            ORDER BY timestamp ASC, seq ASC
        """
        values: list[Any] = []
        if limit > 0:
            sql += " LIMIT ?"
            values.append(int(limit))
        rows = self._connection.execute(sql, values).fetchall()
        return [_capture_from_row(row) for row in rows]

    def list_session_captures(
        self,
        session_id: str,
        *,
        include_duplicates: bool = False,
        pending_only: bool = False,
    ) -> list[TimelineCapture]:
        sql = """
            SELECT * FROM timeline_captures
            WHERE session_id = ?
              AND skipped = 0
        """
        values: list[Any] = [str(session_id)]
        if not include_duplicates:
            sql += " AND need_duplicate = 0"
        if pending_only:
            sql += " AND summary_id = ''"
        sql += " ORDER BY timestamp ASC, seq ASC"
        rows = self._connection.execute(sql, values).fetchall()
        return [_capture_from_row(row) for row in rows]

    def list_summary_captures(self, summary_id: str) -> list[TimelineCapture]:
        """按送审顺序列出某个 timeline run 已消费的 capture。"""
        clean_id = str(summary_id or "").strip()
        if not clean_id:
            return []
        rows = self._connection.execute(
            """
            SELECT * FROM timeline_captures
            WHERE summary_id = ?
              AND skipped = 0
              AND need_duplicate = 0
            ORDER BY timestamp ASC, seq ASC
            """,
            (clean_id,),
        ).fetchall()
        return [_capture_from_row(row) for row in rows]

    def list_all_session_captures(self, session_id: str) -> list[TimelineCapture]:
        rows = self._connection.execute(
            """
            SELECT * FROM timeline_captures
            WHERE session_id = ?
            ORDER BY timestamp ASC, seq ASC
            """,
            (str(session_id),),
        ).fetchall()
        return [_capture_from_row(row) for row in rows]

    def delete_session_captures(self, session_id: str) -> list[str]:
        """删除未送审 session 的 captures，并返回已无其他引用的截图路径。"""
        clean_session_id = str(session_id or "").strip()
        if not clean_session_id:
            return []
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            rows = self._connection.execute(
                """
                SELECT DISTINCT screenshot_path
                FROM timeline_captures
                WHERE session_id = ?
                  AND screenshot_path <> ''
                """,
                (clean_session_id,),
            ).fetchall()
            screenshot_paths = [str(row["screenshot_path"]) for row in rows]
            self._connection.execute(
                "DELETE FROM timeline_captures WHERE session_id = ?",
                (clean_session_id,),
            )
            unreferenced_paths = [
                path
                for path in screenshot_paths
                if self._connection.execute(
                    "SELECT 1 FROM timeline_captures WHERE screenshot_path = ? LIMIT 1",
                    (path,),
                ).fetchone()
                is None
            ]
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        return unreferenced_paths

    def list_screenshot_paths(self) -> set[str]:
        rows = self._connection.execute(
            """
            SELECT DISTINCT screenshot_path
            FROM timeline_captures
            WHERE screenshot_path <> ''
            """
        ).fetchall()
        return {str(row["screenshot_path"]) for row in rows}

    def list_session_days(self) -> list[str]:
        """有 session 记录的日期(YYYY-MM-DD)，新→旧。只读，供 UI 用。"""
        rows = self._connection.execute(
            """
            SELECT DISTINCT substr(started_at, 1, 10) AS day
            FROM timeline_sessions
            WHERE started_at != ''
            ORDER BY day DESC
            """
        ).fetchall()
        return [str(row["day"]) for row in rows if str(row["day"] or "")]

    def list_sessions_by_day(self, day: str) -> list[TimelineSession]:
        """某天的所有 session，按开始时间升序。只读，供 UI 用。"""
        rows = self._connection.execute(
            """
            SELECT * FROM timeline_sessions
            WHERE substr(started_at, 1, 10) = ?
            ORDER BY started_at ASC, seq ASC
            """,
            (str(day),),
        ).fetchall()
        return [_session_from_row(row) for row in rows]

    def list_sessions(self) -> list[TimelineSession]:
        rows = self._connection.execute(
            "SELECT * FROM timeline_sessions ORDER BY started_at ASC, seq ASC"
        ).fetchall()
        return [_session_from_row(row) for row in rows]

    def session_group(self, session_ids: list[str]) -> list[TimelineSession]:
        return timeline_session_group(self.list_sessions(), session_ids=session_ids)

    def session_ids_for_summary(self, summary_id: str) -> list[str]:
        clean_id = str(summary_id or "").strip()
        if not clean_id:
            return []
        rows = self._connection.execute(
            """
            SELECT id
            FROM timeline_sessions
            WHERE summary_id = ?
            ORDER BY started_at ASC, seq ASC
            """,
            (clean_id,),
        ).fetchall()
        return [str(row["id"] or "") for row in rows if str(row["id"] or "")]

    def list_captures_by_day(self, day: str, *, include_skipped: bool = False) -> list[TimelineCapture]:
        """某天的所有 capture，按时间升序。只读，供 UI 统计用。"""
        sql = """
            SELECT * FROM timeline_captures
            WHERE substr(timestamp, 1, 10) = ?
        """
        values: list[Any] = [str(day)]
        if not include_skipped:
            sql += " AND skipped = 0"
        sql += " ORDER BY timestamp ASC, seq ASC"
        rows = self._connection.execute(sql, values).fetchall()
        return [_capture_from_row(row) for row in rows]

    def search_captures(self, query: str, *, limit: int = 50) -> list[TimelineCapture]:
        """按无障碍文本/窗口标题/URL 全文匹配 capture，新→旧。只读，供 UI 用。"""
        needle = str(query or "").strip()
        if not needle:
            return []
        like = f"%{needle}%"
        rows = self._connection.execute(
            """
            SELECT * FROM timeline_captures
            WHERE skipped = 0
              AND (ax_text LIKE ? OR window_title LIKE ? OR url LIKE ?)
            ORDER BY timestamp DESC, seq DESC
            LIMIT ?
            """,
            (like, like, like, int(limit)),
        ).fetchall()
        return [_capture_from_row(row) for row in rows]

    def mark_captures_summarized(self, *, capture_ids: list[str], summary_id: str) -> None:
        clean_ids = [str(capture_id) for capture_id in capture_ids if str(capture_id)]
        if not clean_ids:
            return
        with self._connection:
            self._connection.executemany(
                """
                UPDATE timeline_captures
                SET summary_id = ?
                WHERE id = ?
                """,
                [(str(summary_id), capture_id) for capture_id in clean_ids],
            )

    def replace_summary_id(self, *, old_summary_id: str, new_summary_id: str) -> None:
        old_id = str(old_summary_id or "").strip()
        new_id = str(new_summary_id or "").strip()
        if not old_id or not new_id or old_id == new_id:
            return
        with self._connection:
            self._connection.execute(
                """
                UPDATE timeline_captures
                SET summary_id = ?
                WHERE summary_id = ?
                """,
                (new_id, old_id),
            )
            self._connection.execute(
                """
                UPDATE timeline_sessions
                SET summary_id = ?,
                    updated_at = ?
                WHERE summary_id = ?
                """,
                (new_id, user_now_iso(), old_id),
            )

    def create_ready_session(
        self,
        *,
        session_id: str,
        app_name: str,
        bundle_id: str,
        window_title: str,
        window_key: str,
        started_at: str,
        ended_at: str,
        duration_seconds: float,
        capture_ids: list[str],
        non_duplicate_capture_ids: list[str],
    ) -> TimelineSession:
        now = user_now_iso()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO timeline_sessions (
                    id, app_name, bundle_id, window_title, window_key, started_at,
                    ended_at, duration_seconds, capture_ids_json,
                    non_duplicate_capture_ids_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    app_name = excluded.app_name,
                    bundle_id = excluded.bundle_id,
                    window_title = excluded.window_title,
                    window_key = excluded.window_key,
                    started_at = excluded.started_at,
                    ended_at = excluded.ended_at,
                    duration_seconds = excluded.duration_seconds,
                    capture_ids_json = excluded.capture_ids_json,
                    non_duplicate_capture_ids_json = excluded.non_duplicate_capture_ids_json,
                    status = 'ready',
                    summary_id = '',
                    updated_at = excluded.updated_at
                """,
                (
                    str(session_id),
                    str(app_name or ""),
                    str(bundle_id or ""),
                    str(window_title or ""),
                    str(window_key or ""),
                    str(started_at or ""),
                    str(ended_at or ""),
                    float(duration_seconds or 0),
                    json.dumps([str(item) for item in capture_ids], ensure_ascii=False),
                    json.dumps(
                        [str(item) for item in non_duplicate_capture_ids],
                        ensure_ascii=False,
                    ),
                    now,
                    now,
                ),
            )
        session = self.get_session(session_id)
        if session is None:  # pragma: no cover
            raise RuntimeError("timeline session insert did not round-trip")
        return session

    def get_session(self, session_id: str) -> TimelineSession | None:
        row = self._connection.execute(
            "SELECT * FROM timeline_sessions WHERE id = ?",
            (str(session_id),),
        ).fetchone()
        return _session_from_row(row) if row is not None else None

    def claim_next_ready_sessions_for_summary(self) -> list[TimelineSession]:
        """领取一个活动组的全部 ready session。

        已有 session 处于 processing 的活动组整组跳过，这是活动组级别的锁：
        同一组的两批不会并发送审，`summary.py` 的上一批合并逻辑因此保持正确。
        不同活动组之间没有共享状态，允许并发领取。
        """
        selected_ids: list[str] = []
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            rows = self._connection.execute(
                "SELECT * FROM timeline_sessions ORDER BY started_at ASC, seq ASC"
            ).fetchall()
            if not rows:
                self._connection.execute("COMMIT")
                return []
            sessions = [_session_from_row(row) for row in rows]
            for group in group_timeline_sessions(sessions):
                if any(session.status == "processing" for session in group):
                    continue
                ready = [session for session in group if session.status == "ready"]
                if not ready:
                    continue
                first_ready_index = min(group.index(session) for session in ready)
                if any(session.status == "failed" for session in group):
                    continue
                if any(
                    session.status == "completed"
                    for session in group[first_ready_index + 1 :]
                ):
                    continue
                selected_ids = [session.id for session in ready]
                break
            if not selected_ids:
                self._connection.execute("COMMIT")
                return []
            now = user_now_iso()
            self._connection.executemany(
                """
                UPDATE timeline_sessions
                SET status = 'processing',
                    updated_at = ?
                WHERE id = ? AND status = 'ready'
                """,
                [(now, session_id) for session_id in selected_ids],
            )
            self._connection.execute("COMMIT")
        except Exception:
            try:
                self._connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        return [
            session
            for session_id in selected_ids
            if (session := self.get_session(session_id)) is not None
        ]

    def complete_session(self, *, session_id: str, summary_id: str) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE timeline_sessions
                SET status = 'completed',
                    summary_id = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (str(summary_id), user_now_iso(), str(session_id)),
            )

    def release_session(self, *, session_id: str, summary_id: str = "") -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE timeline_sessions
                SET status = 'ready',
                    summary_id = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (str(summary_id or ""), user_now_iso(), str(session_id)),
            )

    def fail_session(self, *, session_id: str, summary_id: str = "") -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE timeline_sessions
                SET status = 'failed',
                    summary_id = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (str(summary_id or ""), user_now_iso(), str(session_id)),
            )

    def reset_session_group(self, *, session_ids: list[str]) -> dict[str, Any]:
        group = self.session_group(session_ids)
        if not group:
            return {"session_ids": [], "summary_ids": [], "capture_count": 0}
        group_ids = [session.id for session in group]
        placeholders = ", ".join("?" for _item in group_ids)
        capture_summary_rows = self._connection.execute(
            f"""
            SELECT DISTINCT summary_id
            FROM timeline_captures
            WHERE session_id IN ({placeholders})
              AND summary_id <> ''
            ORDER BY summary_id
            """,
            group_ids,
        ).fetchall()
        summary_ids = list(
            dict.fromkeys(
                [
                    str(session.summary_id or "").strip()
                    for session in group
                    if str(session.summary_id or "").strip()
                ]
                + [
                    str(row["summary_id"] or "").strip()
                    for row in capture_summary_rows
                    if str(row["summary_id"] or "").strip()
                ]
            )
        )
        now = user_now_iso()
        with self._connection:
            cursor = self._connection.execute(
                f"""
                UPDATE timeline_captures
                SET summary_id = ''
                WHERE session_id IN ({placeholders})
                """,
                group_ids,
            )
            self._connection.execute(
                f"""
                UPDATE timeline_sessions
                SET status = 'ready',
                    summary_id = '',
                    updated_at = ?
                WHERE id IN ({placeholders})
                """,
                [now, *group_ids],
            )
        return {
            "session_ids": group_ids,
            "summary_ids": summary_ids,
            "capture_count": int(cursor.rowcount or 0),
        }

    def release_stale_processing_sessions(self, *, stale_seconds: float) -> int:
        cutoff = user_now() - timedelta(seconds=max(1.0, float(stale_seconds)))
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE timeline_sessions
                SET status = 'ready',
                    updated_at = ?
                WHERE status = 'processing'
                  AND updated_at < ?
                """,
                (user_now_iso(), cutoff.isoformat(timespec="seconds")),
            )
        return int(cursor.rowcount or 0)

    def release_processing_sessions(self) -> int:
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE timeline_sessions
                SET status = 'ready',
                    summary_id = '',
                    updated_at = ?
                WHERE status = 'processing'
                """,
                (user_now_iso(),),
            )
        return int(cursor.rowcount or 0)

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self._connection.execute(
            "SELECT value_json FROM timeline_state WHERE key = ?",
            (str(key),),
        ).fetchone()
        if row is None:
            return default
        return json.loads(str(row["value_json"] or "null"))

    def set_state(self, key: str, value: Any) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO timeline_state(key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (
                    str(key),
                    json.dumps(value, ensure_ascii=False, sort_keys=True, default=str),
                    user_now_iso(),
                ),
            )

    def delete_state(self, key: str) -> None:
        with self._connection:
            self._connection.execute(
                "DELETE FROM timeline_state WHERE key = ?",
                (str(key),),
            )

    def _init_schema(self) -> None:
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS timeline_captures (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    timestamp TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    app_name TEXT NOT NULL,
                    bundle_id TEXT NOT NULL,
                    pid INTEGER NOT NULL,
                    window_title TEXT NOT NULL,
                    window_key TEXT NOT NULL,
                    url TEXT NOT NULL,
                    document TEXT NOT NULL,
                    screenshot_path TEXT NOT NULL,
                    screenshot_sha256 TEXT NOT NULL,
                    image_digest TEXT NOT NULL,
                    ax_text TEXT NOT NULL,
                    ax_digest TEXT NOT NULL,
                    text_source TEXT NOT NULL,
                    needs_vision INTEGER NOT NULL DEFAULT 0,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    need_duplicate INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    summary_id TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_timeline_captures_time ON timeline_captures(timestamp, seq)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_timeline_captures_summary ON timeline_captures(summary_id, timestamp, seq)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_timeline_captures_window ON timeline_captures(window_key, timestamp, seq)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_timeline_captures_session ON timeline_captures(session_id, timestamp, seq)"
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS timeline_sessions (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    app_name TEXT NOT NULL,
                    bundle_id TEXT NOT NULL,
                    window_title TEXT NOT NULL,
                    window_key TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    duration_seconds REAL NOT NULL DEFAULT 0,
                    capture_ids_json TEXT NOT NULL,
                    non_duplicate_capture_ids_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_timeline_sessions_status ON timeline_sessions(status, ended_at, seq)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_timeline_sessions_app ON timeline_sessions(app_name, ended_at, seq)"
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS timeline_state (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )


def _capture_from_row(row: sqlite3.Row) -> TimelineCapture:
    return TimelineCapture(
        seq=int(row["seq"]),
        id=str(row["id"]),
        timestamp=str(row["timestamp"]),
        trigger=str(row["trigger"]),
        app_name=str(row["app_name"]),
        bundle_id=str(row["bundle_id"]),
        pid=int(row["pid"] or 0),
        window_title=str(row["window_title"]),
        window_key=str(row["window_key"]),
        url=str(row["url"]),
        document=str(row["document"]),
        screenshot_path=str(row["screenshot_path"]),
        screenshot_sha256=str(row["screenshot_sha256"]),
        image_digest=str(row["image_digest"]),
        ax_text=str(row["ax_text"]),
        ax_digest=str(row["ax_digest"]),
        text_source=str(row["text_source"]),
        needs_vision=bool(row["needs_vision"]),
        skipped=bool(row["skipped"]),
        need_duplicate=bool(row["need_duplicate"]),
        error=str(row["error"]),
        session_id=str(row["session_id"] or ""),
        summary_id=str(row["summary_id"] or ""),
        payload=_loads_object(row["payload_json"]),
    )


def _session_from_row(row: sqlite3.Row) -> TimelineSession:
    return TimelineSession(
        seq=int(row["seq"]),
        id=str(row["id"]),
        app_name=str(row["app_name"] or ""),
        bundle_id=str(row["bundle_id"] or ""),
        window_title=str(row["window_title"] or ""),
        window_key=str(row["window_key"] or ""),
        started_at=str(row["started_at"] or ""),
        ended_at=str(row["ended_at"] or ""),
        duration_seconds=float(row["duration_seconds"] or 0),
        capture_ids=[str(item) for item in list(_loads_array(row["capture_ids_json"]))],
        non_duplicate_capture_ids=[
            str(item) for item in list(_loads_array(row["non_duplicate_capture_ids_json"]))
        ],
        status=str(row["status"] or ""),
        summary_id=str(row["summary_id"] or ""),
        created_at=str(row["created_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
    )


def _loads_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _loads_array(raw: str) -> list[Any]:
    try:
        value = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def _json_string(value: Any) -> str:
    if value in (None, "", {}, []):
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
