"""activity.sqlite repository：活动、片段归属、变更记录和环路水位线。

活动图谱的事实库，位于智能体目录。memory space 里的
`knowledge/knowledge.sqlite` 是由它生成的投影，见 `telos.activity.projection`。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from telos.core.clock import parse_user_time, user_now_iso
from telos.core.ids import generate_id
from telos.storage.filesystem import ensure_dir

MISC_ACTIVITY_ID = "act_misc"
"""提示词里固定拼上的占位活动。它不在活动池中，`activities` 表没有这一行。"""

STATUS_OPEN = "open"
STATUS_SEALED = "sealed"
STATUS_MERGED = "merged"


@dataclass(frozen=True)
class Activity:
    id: str
    status: str
    title: str
    description: str
    span_start: str
    span_end: str
    active_seconds: float
    superseded_by: str = ""
    split_from: str = ""
    sealed_at: str = ""
    created_at: str = ""
    updated_at: str = ""

    @property
    def density(self) -> float:
        """`active_seconds` 除以包络。同一行三个值的纯函数，不落列。

        `span_end` 等于 `span_start` 时记为 1。没有 detail 时记为 0。
        """
        if not self.span_start or not self.span_end:
            return 0.0
        try:
            envelope = (
                parse_user_time(self.span_end) - parse_user_time(self.span_start)
            ).total_seconds()
        except ValueError:
            return 0.0
        if envelope <= 0:
            return 1.0
        return min(1.0, self.active_seconds / envelope)


@dataclass(frozen=True)
class ActivityCapture:
    activity_id: str
    capture_id: str
    timeline_id: str
    detail_index: int
    start_time: str
    end_time: str


@dataclass(frozen=True)
class ActivityRevision:
    seq: int
    id: str
    activity_id: str
    action: str
    actor: str
    payload: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class DetailAssignment:
    """一条 detail 的归属结果，写库前的中间形态。"""

    activity_id: str
    capture_ids: list[str]
    timeline_id: str
    detail_index: int
    start_time: str
    end_time: str


class ActivityRepository:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()
        ensure_dir(self.path.parent)
        self._connection = sqlite3.connect(str(self.path))
        self._connection.row_factory = sqlite3.Row
        self._init_schema()

    @classmethod
    def for_agent_dir(cls, agent_dir: Path) -> "ActivityRepository":
        return cls(Path(agent_dir) / "runtime" / "activity.sqlite")

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "ActivityRepository":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.close()

    # ---- 环路状态 -------------------------------------------------------

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self._connection.execute(
            "SELECT value_json FROM activity_state WHERE key = ?",
            (str(key),),
        ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(str(row["value_json"]))
        except ValueError:
            return default

    def set_state(self, key: str, value: Any) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO activity_state (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (str(key), json.dumps(value, ensure_ascii=False), user_now_iso()),
            )

    # ---- 活动读取 -------------------------------------------------------

    def get_activity(self, activity_id: str) -> Activity | None:
        row = self._connection.execute(
            "SELECT * FROM activities WHERE id = ?",
            (str(activity_id),),
        ).fetchone()
        return _activity_from_row(row) if row is not None else None

    def list_open_activities(self) -> list[Activity]:
        rows = self._connection.execute(
            "SELECT * FROM activities WHERE status = ? ORDER BY span_start ASC, id ASC",
            (STATUS_OPEN,),
        ).fetchall()
        return [_activity_from_row(row) for row in rows]

    def list_activities_overlapping(
        self,
        *,
        start_time: str,
        end_time: str,
        statuses: tuple[str, ...] = (STATUS_OPEN, STATUS_SEALED),
    ) -> list[Activity]:
        """返回 span 与 [start_time, end_time] 相交的活动，按 span_start 升序。"""
        placeholders = ", ".join("?" for _ in statuses)
        rows = self._connection.execute(
            f"""
            SELECT * FROM activities
            WHERE status IN ({placeholders})
              AND span_start <= ?
              AND span_end >= ?
            ORDER BY span_start ASC, id ASC
            """,
            (*statuses, str(end_time), str(start_time)),
        ).fetchall()
        return [_activity_from_row(row) for row in rows]

    def list_activity_captures(self, activity_id: str) -> list[ActivityCapture]:
        """按时间升序返回片段。

        同一条 detail 的多行按写入顺序排列，因此每组的第一行就是该 detail 的
        `capture_ids[0]`，与 `detail_id` 的定义一致。合并和拆分只改 activity_id，
        不改 rowid，写入顺序始终保留。
        """
        rows = self._connection.execute(
            """
            SELECT * FROM activity_captures
            WHERE activity_id = ?
            ORDER BY start_time ASC, rowid ASC
            """,
            (str(activity_id),),
        ).fetchall()
        return [_capture_from_row(row) for row in rows]

    def assigned_activity_ids(self, capture_ids: list[str]) -> dict[str, str]:
        """返回已归属的 capture_id -> activity_id。重新送审时用它跳过模型。"""
        clean = [str(item) for item in capture_ids if str(item or "").strip()]
        if not clean:
            return {}
        placeholders = ", ".join("?" for _ in clean)
        rows = self._connection.execute(
            f"SELECT capture_id, activity_id FROM activity_captures "
            f"WHERE capture_id IN ({placeholders})",
            clean,
        ).fetchall()
        return {str(row["capture_id"]): str(row["activity_id"]) for row in rows}

    def list_revisions(self, activity_id: str) -> list[ActivityRevision]:
        rows = self._connection.execute(
            """
            SELECT * FROM activity_revisions
            WHERE activity_id = ?
            ORDER BY seq ASC
            """,
            (str(activity_id),),
        ).fetchall()
        return [_revision_from_row(row) for row in rows]

    # ---- 活动写入 -------------------------------------------------------

    def create_activity(
        self,
        *,
        title: str,
        description: str = "",
        activity_id: str = "",
        split_from: str = "",
    ) -> str:
        new_id = str(activity_id or "").strip() or generate_id("act")
        now = user_now_iso()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO activities (
                    id, status, title, description,
                    span_start, span_end, active_seconds,
                    superseded_by, split_from, sealed_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, '', '', 0, '', ?, '', ?, ?)
                """,
                (
                    new_id,
                    STATUS_OPEN,
                    str(title),
                    str(description or ""),
                    str(split_from or ""),
                    now,
                    now,
                ),
            )
        return new_id

    def update_activity_text(
        self,
        activity_id: str,
        *,
        title: str = "",
        description: str = "",
    ) -> None:
        """只覆盖非空字段，空字符串表示保持原值。"""
        clean_title = str(title or "").strip()
        clean_description = str(description or "").strip()
        if not clean_title and not clean_description:
            return
        assignments = []
        values: list[Any] = []
        if clean_title:
            assignments.append("title = ?")
            values.append(clean_title)
        if clean_description:
            assignments.append("description = ?")
            values.append(clean_description)
        assignments.append("updated_at = ?")
        values.extend([user_now_iso(), str(activity_id)])
        with self._connection:
            self._connection.execute(
                f"UPDATE activities SET {', '.join(assignments)} WHERE id = ?",
                values,
            )

    def attach_details(self, assignments: list[DetailAssignment]) -> None:
        """把 detail 的每个 capture 写成一行。同一 capture 重复归属为覆盖。"""
        rows = [
            (
                assignment.activity_id,
                capture_id,
                assignment.timeline_id,
                int(assignment.detail_index),
                assignment.start_time,
                assignment.end_time,
            )
            for assignment in assignments
            for capture_id in assignment.capture_ids
        ]
        if not rows:
            return
        with self._connection:
            # capture_id 是唯一键，同一 capture 换活动时先删旧行再写新行。
            self._connection.executemany(
                "DELETE FROM activity_captures WHERE capture_id = ?",
                [(row[1],) for row in rows],
            )
            self._connection.executemany(
                """
                INSERT INTO activity_captures (
                    activity_id, capture_id, timeline_id, detail_index, start_time, end_time
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

    def recompute_spans(self, activity_ids: list[str]) -> None:
        for activity_id in dict.fromkeys(
            str(item) for item in activity_ids if str(item or "").strip()
        ):
            if activity_id == MISC_ACTIVITY_ID:
                continue
            self._recompute_span(activity_id)

    def seal_activity(self, activity_id: str, *, sealed_at: str, actor: str) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE activities
                SET status = ?, sealed_at = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    STATUS_SEALED,
                    str(sealed_at),
                    user_now_iso(),
                    str(activity_id),
                    STATUS_OPEN,
                ),
            )
        self.append_revision(
            activity_id=activity_id,
            action="seal",
            actor=actor,
            payload={"sealed_at": str(sealed_at)},
        )

    def merge_activities(self, *, keep_id: str, merged_ids: list[str], actor: str) -> None:
        """把 merged_ids 的片段迁到 keep_id，被合并的活动置 merged 并写 superseded_by。"""
        targets = [
            str(item)
            for item in merged_ids
            if str(item or "").strip() and str(item) != str(keep_id)
        ]
        if not targets:
            return
        keep = self.get_activity(keep_id)
        if keep is None:
            raise ValueError(f"unknown activity {keep_id!r}")
        payload: dict[str, Any] = {
            "keep": {
                "activity_id": keep.id,
                "title": keep.title,
                "description": keep.description,
                "status": keep.status,
                "sealed_at": keep.sealed_at,
            },
            "merged": [],
        }
        now = user_now_iso()
        sealed_at_values = [keep.sealed_at] if keep.sealed_at else []
        any_open = keep.status == STATUS_OPEN
        with self._connection:
            for activity_id in targets:
                source = self.get_activity(activity_id)
                if source is None:
                    raise ValueError(f"unknown activity {activity_id!r}")
                captures = self.list_activity_captures(activity_id)
                rechained = [
                    str(row["id"])
                    for row in self._connection.execute(
                        "SELECT id FROM activities WHERE superseded_by = ? AND id != ?",
                        (activity_id, str(keep_id)),
                    ).fetchall()
                ]
                payload["merged"].append(
                    {
                        "activity_id": activity_id,
                        "title": source.title,
                        "description": source.description,
                        "status": source.status,
                        "sealed_at": source.sealed_at,
                        "capture_ids": [item.capture_id for item in captures],
                        "rechained": rechained,
                    }
                )
                if source.status == STATUS_OPEN:
                    any_open = True
                elif source.sealed_at:
                    sealed_at_values.append(source.sealed_at)
                self._connection.execute(
                    "UPDATE activity_captures SET activity_id = ? WHERE activity_id = ?",
                    (str(keep_id), activity_id),
                )
                self._connection.execute(
                    """
                    UPDATE activities
                    SET status = ?, superseded_by = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (STATUS_MERGED, str(keep_id), now, activity_id),
                )
                # 连续合并时改写整条链，解析旧 id 时无需递归。
                self._connection.execute(
                    """
                    UPDATE activities
                    SET superseded_by = ?, updated_at = ?
                    WHERE superseded_by = ? AND id != ?
                    """,
                    (str(keep_id), now, activity_id, str(keep_id)),
                )
            status = STATUS_OPEN if any_open else STATUS_SEALED
            sealed_at = "" if any_open else (max(sealed_at_values) if sealed_at_values else "")
            self._connection.execute(
                "UPDATE activities SET status = ?, sealed_at = ?, updated_at = ? WHERE id = ?",
                (status, sealed_at, now, str(keep_id)),
            )
        self._recompute_span(keep_id)
        self.append_revision(
            activity_id=keep_id,
            action="merge",
            actor=actor,
            payload=payload,
        )

    def split_activity(
        self,
        *,
        activity_id: str,
        segments: list[dict[str, Any]],
        actor: str,
    ) -> list[str]:
        """按 detail 列表分段。含活动首个 detail 的段保留原 id，其余新建并写 split_from。

        segments 每项含 detail_ids、title、description。detail_ids 合起来必须是该活动
        全部 detail 的一个划分，由调用方校验；同一条 detail 的多行一起迁移。
        """
        source = self.get_activity(activity_id)
        if source is None:
            raise ValueError(f"unknown activity {activity_id!r}")
        captures = self.list_activity_captures(activity_id)
        detail_keys: dict[str, tuple[str, int]] = {}
        first_detail_id = ""
        for capture in captures:
            key = (capture.timeline_id, capture.detail_index)
            if key in detail_keys.values():
                continue
            if not first_detail_id:
                first_detail_id = capture.capture_id
            detail_keys[capture.capture_id] = key
        segment_detail_ids = [
            [str(value) for value in list(segment.get("detail_ids") or [])]
            for segment in segments
        ]
        for detail_ids in segment_detail_ids:
            for detail_id in detail_ids:
                if detail_id not in detail_keys:
                    raise ValueError(f"unknown split detail {detail_id!r}")
        buckets = [
            [
                capture
                for capture in captures
                if (capture.timeline_id, capture.detail_index)
                in {detail_keys[detail_id] for detail_id in detail_ids}
            ]
            for detail_ids in segment_detail_ids
        ]
        keep_index = next(
            index
            for index, detail_ids in enumerate(segment_detail_ids)
            if first_detail_id in detail_ids
        )
        payload = {
            "title": source.title,
            "description": source.description,
            "segments": [
                {
                    "detail_ids": detail_ids,
                    "capture_ids": [item.capture_id for item in bucket],
                }
                for detail_ids, bucket in zip(segment_detail_ids, buckets, strict=True)
            ],
        }
        keep_segment = segments[keep_index]
        self.update_activity_text(
            activity_id,
            title=str(keep_segment.get("title") or ""),
            description=str(keep_segment.get("description") or ""),
        )
        result_ids = [str(activity_id)]
        for segment, bucket in zip(segments, buckets, strict=True):
            if segment is keep_segment:
                continue
            new_id = self.create_activity(
                title=str(segment.get("title") or source.title),
                description=str(segment.get("description") or ""),
                split_from=str(activity_id),
            )
            with self._connection:
                self._connection.executemany(
                    "UPDATE activity_captures SET activity_id = ? WHERE capture_id = ?",
                    [(new_id, item.capture_id) for item in bucket],
                )
                self._connection.execute(
                    "UPDATE activities SET status = ?, sealed_at = ?, updated_at = ? WHERE id = ?",
                    (source.status, source.sealed_at, user_now_iso(), new_id),
                )
            result_ids.append(new_id)
        self.recompute_spans(result_ids)
        self.append_revision(
            activity_id=activity_id,
            action="split",
            actor=actor,
            payload={**payload, "activity_ids": result_ids},
        )
        return result_ids

    def get_revision(self, revision_id: str) -> ActivityRevision | None:
        row = self._connection.execute(
            "SELECT * FROM activity_revisions WHERE id = ?",
            (str(revision_id),),
        ).fetchone()
        return _revision_from_row(row) if row is not None else None

    def undo_revision(self, revision_id: str) -> str:
        """反向执行一次慢环路变更，返回被撤销的 action。

        只支持 merge、split、relabel 三种。extend 撤销后片段会失去归属而不会被重新
        送审；关闭是终态，不设重新打开。
        """
        revision = self.get_revision(revision_id)
        if revision is None:
            raise ValueError(f"unknown revision {revision_id!r}")
        pending = self._later_revisions(revision)
        if pending:
            raise ValueError(
                f"revision {revision_id!r} has later changes; undo {pending[0]!r} first"
            )
        if revision.action == "merge":
            self._undo_merge(revision)
        elif revision.action == "split":
            self._undo_split(revision)
        elif revision.action == "relabel":
            self._undo_relabel(revision)
        else:
            raise ValueError(f"revision action {revision.action!r} cannot be undone")
        self.append_revision(
            activity_id=revision.activity_id,
            action="undo",
            actor="user",
            payload={"revision_id": revision.id, "action": revision.action},
        )
        return revision.action

    def _later_revisions(self, revision: ActivityRevision) -> list[str]:
        """同一条活动上更晚的改动。撤销必须从最后一次开始，否则会按过期的快照回滚。

        `undo` 本身不算改动；已被撤销过的变更由它对应的 undo 记录标记。
        """
        rows = self._connection.execute(
            """
            SELECT id, action, payload_json FROM activity_revisions
            WHERE activity_id = ? AND seq > ?
            ORDER BY seq ASC
            """,
            (revision.activity_id, int(revision.seq)),
        ).fetchall()
        undone: set[str] = set()
        later: list[str] = []
        for row in rows:
            if str(row["action"]) == "undo":
                try:
                    payload = json.loads(str(row["payload_json"] or "{}"))
                except ValueError:
                    payload = {}
                undone.add(str(payload.get("revision_id") or ""))
                continue
            later.append(str(row["id"]))
        if revision.id in undone:
            raise ValueError(f"revision {revision.id!r} was already undone")
        return [item for item in later if item not in undone]

    def _undo_merge(self, revision: ActivityRevision) -> None:
        keep = revision.payload.get("keep") or {}
        keep_id = str(keep.get("activity_id") or revision.activity_id)
        touched = [keep_id]
        now = user_now_iso()
        with self._connection:
            for item in list(revision.payload.get("merged") or []):
                activity_id = str(item.get("activity_id") or "")
                if not activity_id:
                    continue
                capture_ids = [str(value) for value in list(item.get("capture_ids") or [])]
                if capture_ids:
                    self._connection.executemany(
                        "UPDATE activity_captures SET activity_id = ? WHERE capture_id = ?",
                        [(activity_id, capture_id) for capture_id in capture_ids],
                    )
                self._connection.execute(
                    """
                    UPDATE activities
                    SET status = ?, sealed_at = ?, title = ?, description = ?,
                        superseded_by = '', updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        str(item.get("status") or STATUS_OPEN),
                        str(item.get("sealed_at") or ""),
                        str(item.get("title") or ""),
                        str(item.get("description") or ""),
                        now,
                        activity_id,
                    ),
                )
                for chained in list(item.get("rechained") or []):
                    self._connection.execute(
                        "UPDATE activities SET superseded_by = ?, updated_at = ? WHERE id = ?",
                        (activity_id, now, str(chained)),
                    )
                touched.append(activity_id)
            self._connection.execute(
                """
                UPDATE activities
                SET status = ?, sealed_at = ?, title = ?, description = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    str(keep.get("status") or STATUS_OPEN),
                    str(keep.get("sealed_at") or ""),
                    str(keep.get("title") or ""),
                    str(keep.get("description") or ""),
                    now,
                    keep_id,
                ),
            )
        self.recompute_spans(touched)

    def _undo_split(self, revision: ActivityRevision) -> None:
        origin_id = revision.activity_id
        segment_ids = [str(value) for value in list(revision.payload.get("activity_ids") or [])]
        extras = [item for item in segment_ids if item != origin_id]
        with self._connection:
            for activity_id in extras:
                self._connection.execute(
                    "UPDATE activity_captures SET activity_id = ? WHERE activity_id = ?",
                    (origin_id, activity_id),
                )
                self._connection.execute("DELETE FROM activities WHERE id = ?", (activity_id,))
            self._connection.execute(
                "UPDATE activities SET title = ?, description = ?, updated_at = ? WHERE id = ?",
                (
                    str(revision.payload.get("title") or ""),
                    str(revision.payload.get("description") or ""),
                    user_now_iso(),
                    origin_id,
                ),
            )
        self.recompute_spans([origin_id])

    def _undo_relabel(self, revision: ActivityRevision) -> None:
        before = revision.payload.get("before") or {}
        with self._connection:
            self._connection.execute(
                "UPDATE activities SET title = ?, description = ?, updated_at = ? WHERE id = ?",
                (
                    str(before.get("title") or ""),
                    str(before.get("description") or ""),
                    user_now_iso(),
                    revision.activity_id,
                ),
            )

    def append_revision(
        self,
        *,
        activity_id: str,
        action: str,
        actor: str,
        payload: dict[str, Any],
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO activity_revisions (
                    id, activity_id, action, actor, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    generate_id("arev"),
                    str(activity_id),
                    str(action),
                    str(actor),
                    json.dumps(payload, ensure_ascii=False),
                    user_now_iso(),
                ),
            )

    # ---- 内部 -----------------------------------------------------------

    def _recompute_span(self, activity_id: str) -> None:
        captures = self.list_activity_captures(activity_id)
        intervals = _detail_intervals(captures)
        if not intervals:
            span_start = span_end = ""
            active_seconds = 0.0
        else:
            span_start = min(start for start, _ in intervals).isoformat(timespec="seconds")
            span_end = max(end for _, end in intervals).isoformat(timespec="seconds")
            active_seconds = _union_seconds(intervals)
        with self._connection:
            self._connection.execute(
                """
                UPDATE activities
                SET span_start = ?, span_end = ?, active_seconds = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    span_start,
                    span_end,
                    float(active_seconds),
                    user_now_iso(),
                    str(activity_id),
                ),
            )

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
                  span_start     TEXT NOT NULL DEFAULT '',
                  span_end       TEXT NOT NULL DEFAULT '',
                  active_seconds REAL NOT NULL DEFAULT 0,
                  superseded_by  TEXT NOT NULL DEFAULT '',
                  split_from     TEXT NOT NULL DEFAULT '',
                  sealed_at      TEXT NOT NULL DEFAULT '',
                  created_at     TEXT NOT NULL,
                  updated_at     TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_activities_span "
                "ON activities(span_start, span_end)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_activities_open ON activities(status, span_end)"
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS activity_captures (
                  activity_id  TEXT NOT NULL,
                  capture_id   TEXT NOT NULL,
                  timeline_id  TEXT NOT NULL,
                  detail_index INTEGER NOT NULL,
                  start_time   TEXT NOT NULL,
                  end_time     TEXT NOT NULL,
                  PRIMARY KEY (activity_id, capture_id),
                  UNIQUE (capture_id)
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_activity_captures_timeline "
                "ON activity_captures(timeline_id)"
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS activity_revisions (
                  seq          INTEGER PRIMARY KEY AUTOINCREMENT,
                  id           TEXT NOT NULL UNIQUE,
                  activity_id  TEXT NOT NULL,
                  action       TEXT NOT NULL,
                  actor        TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  created_at   TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_activity_revisions_activity "
                "ON activity_revisions(activity_id, seq)"
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS activity_state (
                  key        TEXT PRIMARY KEY,
                  value_json TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )


def detail_ids_for(captures: list[ActivityCapture]) -> list[str]:
    """一条活动包含的 detail_id，按时间升序。

    输入取自 `list_activity_captures`，同一条 detail 的多行已按写入顺序排列，
    因此每组的第一行就是 detail_id。
    """
    seen: set[tuple[str, int]] = set()
    ordered: list[str] = []
    for capture in captures:
        key = (capture.timeline_id, capture.detail_index)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(capture.capture_id)
    return ordered


def _detail_intervals(captures: list[ActivityCapture]) -> list[tuple[datetime, datetime]]:
    """同一 detail 的多个 capture 共享区间，去重后返回。"""
    seen: set[tuple[str, str]] = set()
    intervals: list[tuple[datetime, datetime]] = []
    for capture in captures:
        key = (capture.start_time, capture.end_time)
        if key in seen:
            continue
        seen.add(key)
        try:
            start = parse_user_time(capture.start_time)
            end = parse_user_time(capture.end_time)
        except ValueError:
            continue
        intervals.append((start, end) if end >= start else (end, start))
    return intervals


def _union_seconds(intervals: list[tuple[datetime, datetime]]) -> float:
    total = 0.0
    current_start: datetime | None = None
    current_end: datetime | None = None
    for start, end in sorted(intervals):
        if current_end is None or start > current_end:
            if current_start is not None and current_end is not None:
                total += (current_end - current_start).total_seconds()
            current_start, current_end = start, end
            continue
        current_end = max(current_end, end)
    if current_start is not None and current_end is not None:
        total += (current_end - current_start).total_seconds()
    return total


def _activity_from_row(row: sqlite3.Row) -> Activity:
    return Activity(
        id=str(row["id"]),
        status=str(row["status"]),
        title=str(row["title"]),
        description=str(row["description"] or ""),
        span_start=str(row["span_start"] or ""),
        span_end=str(row["span_end"] or ""),
        active_seconds=float(row["active_seconds"] or 0),
        superseded_by=str(row["superseded_by"] or ""),
        split_from=str(row["split_from"] or ""),
        sealed_at=str(row["sealed_at"] or ""),
        created_at=str(row["created_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
    )


def _capture_from_row(row: sqlite3.Row) -> ActivityCapture:
    return ActivityCapture(
        activity_id=str(row["activity_id"]),
        capture_id=str(row["capture_id"]),
        timeline_id=str(row["timeline_id"] or ""),
        detail_index=int(row["detail_index"] or 0),
        start_time=str(row["start_time"] or ""),
        end_time=str(row["end_time"] or ""),
    )


def _revision_from_row(row: sqlite3.Row) -> ActivityRevision:
    try:
        payload = json.loads(str(row["payload_json"] or "{}"))
    except ValueError:
        payload = {}
    return ActivityRevision(
        seq=int(row["seq"]),
        id=str(row["id"]),
        activity_id=str(row["activity_id"]),
        action=str(row["action"]),
        actor=str(row["actor"]),
        payload=payload if isinstance(payload, dict) else {},
        created_at=str(row["created_at"]),
    )
