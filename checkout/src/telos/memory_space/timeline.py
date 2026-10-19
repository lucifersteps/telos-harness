"""timeline.sqlite repository and normalization."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from telos.core.clock import parse_user_time, user_now_iso
from telos.core.ids import generate_id
from telos.storage.filesystem import ensure_dir


FAILED_SUMMARY = "处理失败"
_SCREENSHOT_RE = re.compile(
    r"^(?P<hour>\d{2})-(?P<minute>\d{2})-(?P<second>\d{2})"
    r"(?:-(?P<microsecond>\d{6})-[0-9a-f]{32})?\.(jpg|jpeg|png|webp)$",
    re.IGNORECASE,
)
_JSON_CODE_FENCE_RE = re.compile(
    r"\A```(?:json)?\s*(?P<payload>\{.*\})\s*```\Z",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class TimelineSummaryRecord:
    seq: int
    timeline_id: str
    app_name: str
    start_time: str
    end_time: str
    summary: str
    details: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""


@dataclass(frozen=True)
class NormalizedTimelineResponse:
    app: str
    summary: str
    details: list[dict[str, Any]]


class TimelineSummaryRepository:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()
        ensure_dir(self.path.parent)
        self._connection = sqlite3.connect(str(self.path))
        self._connection.row_factory = sqlite3.Row
        self._init_schema()

    @classmethod
    def for_memory_space(cls, root: Path) -> "TimelineSummaryRepository":
        return cls(Path(root) / "timeline" / "timeline.sqlite")

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "TimelineSummaryRepository":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.close()

    def append(
        self,
        *,
        start_time: str,
        end_time: str,
        summary: str,
        details: list[dict[str, Any]],
        timeline_id: str = "",
        app_name: str = "",
        created_at: str = "",
    ) -> TimelineSummaryRecord:
        record_id = timeline_id or generate_id("timeline")
        timestamp = created_at or user_now_iso()
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO timeline_summaries (
                    timeline_id, app_name, start_time, end_time, summary, details, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    str(app_name or ""),
                    str(start_time),
                    str(end_time),
                    str(summary),
                    _details_json(details),
                    timestamp,
                ),
            )
        row = self._connection.execute(
            "SELECT * FROM timeline_summaries WHERE seq = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
        return _summary_from_row(row)

    def append_failure(
        self,
        *,
        start_time: str,
        end_time: str,
        timeline_id: str = "",
        app_name: str = "",
    ) -> TimelineSummaryRecord:
        return self.append(
            start_time=start_time,
            end_time=end_time,
            summary=FAILED_SUMMARY,
            details=[],
            timeline_id=timeline_id,
            app_name=app_name,
        )

    def delete(self, timeline_id: str) -> None:
        clean_id = str(timeline_id or "").strip()
        if not clean_id:
            return
        with self._connection:
            self._connection.execute(
                "DELETE FROM timeline_summaries WHERE timeline_id = ?",
                (clean_id,),
            )

    def list(self, *, day: str = "") -> list[TimelineSummaryRecord]:
        sql = "SELECT * FROM timeline_summaries"
        values: list[Any] = []
        if day:
            sql += " WHERE substr(start_time, 1, 10) = ?"
            values.append(str(day))
        sql += " ORDER BY start_time ASC, seq ASC"
        rows = self._connection.execute(sql, values).fetchall()
        return [_summary_from_row(row) for row in rows]

    def runs_after(self, *, seq: int, limit: int = 0) -> list[TimelineSummaryRecord]:
        """按 seq 升序返回水位线之后的 timeline run，供活动层快环路取数。

        run 合并会删除旧行并插入新行，新行的 seq 更大，因此合并结果一定在水位线之后。
        """
        sql = "SELECT * FROM timeline_summaries WHERE seq > ? ORDER BY seq ASC"
        values: list[Any] = [int(seq)]
        if limit > 0:
            sql += " LIMIT ?"
            values.append(int(limit))
        rows = self._connection.execute(sql, values).fetchall()
        return [_summary_from_row(row) for row in rows]

    def get(self, timeline_id: str) -> TimelineSummaryRecord | None:
        clean_id = str(timeline_id or "").strip()
        if not clean_id:
            return None
        row = self._connection.execute(
            "SELECT * FROM timeline_summaries WHERE timeline_id = ?",
            (clean_id,),
        ).fetchone()
        return _summary_from_row(row) if row is not None else None

    def _init_schema(self) -> None:
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS timeline_summaries (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  timeline_id TEXT NOT NULL UNIQUE,
                  app_name TEXT NOT NULL DEFAULT '',

                  start_time TEXT NOT NULL,
                  end_time TEXT NOT NULL,

                  summary TEXT NOT NULL,
                  details TEXT NOT NULL DEFAULT '[]',

                  created_at TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_timeline_summaries_time ON timeline_summaries(start_time, seq)"
            )

def timeline_run_context_uri(timeline_id: str) -> str:
    clean = str(timeline_id or "").strip()
    if not clean:
        raise ValueError("timeline_id is required")
    return f"memory_space://timeline/runs/{clean}/context.jsonl"


def timeline_run_json_uri(timeline_id: str) -> str:
    clean = str(timeline_id or "").strip()
    if not clean:
        raise ValueError("timeline_id is required")
    return f"memory_space://timeline/runs/{clean}/run.json"


def normalize_timeline_response(
    response: str,
    *,
    capture_ids_by_screenshot_id: dict[str, str],
    source_end_time: str,
) -> NormalizedTimelineResponse:
    """校验模型输出，并把提示词里的 screenshot_id 解析回稳定的 capture_id。"""
    data = decode_timeline_response_object(response)
    app = _required_text(data, "app")
    summary = _required_text(data, "summary")
    raw_details = data.get("details")
    if not isinstance(raw_details, list):
        raise ValueError("timeline summary response must contain details array")

    final_end_time = _required_text({"source_end_time": source_end_time}, "source_end_time")
    capture_ids = {
        str(key): str(value)
        for key, value in dict(capture_ids_by_screenshot_id or {}).items()
        if str(key)
    }
    allowed = set(capture_ids)
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    start_times: list[str] = []
    for item in raw_details:
        if not isinstance(item, dict):
            raise ValueError("timeline detail must be an object")
        if "end_time" in item:
            raise ValueError("timeline detail must not contain end_time")
        start_times.append(_required_text(item, "start_time"))
    _validate_detail_start_times(start_times, final_end_time=final_end_time)

    for index, item in enumerate(raw_details):
        if not isinstance(item, dict):
            raise ValueError("timeline detail must be an object")
        screenshot_ids = item.get("screenshot_ids")
        if not isinstance(screenshot_ids, list) or not screenshot_ids:
            raise ValueError("timeline detail must contain screenshot_ids")
        clean_ids = [str(value) for value in screenshot_ids if str(value)]
        if not clean_ids:
            raise ValueError("timeline detail must contain screenshot_ids")
        unknown = [value for value in clean_ids if value not in allowed]
        if unknown:
            raise ValueError(f"unknown timeline screenshot_id {unknown[0]!r}")
        for value in clean_ids:
            if value in seen:
                raise ValueError(f"duplicate timeline screenshot_id {value!r}")
            seen.add(value)
        description = item.get("description")
        normalized.append(
            {
                "start_time": start_times[index],
                "end_time": start_times[index + 1]
                if index + 1 < len(start_times)
                else final_end_time,
                "description": None
                if description is None
                else str(description or "").strip(),
                "capture_ids": [capture_ids[value] for value in clean_ids],
            }
        )
    missing = sorted(allowed - seen)
    if missing:
        raise ValueError(f"missing timeline screenshot_id {missing[0]!r}")
    return NormalizedTimelineResponse(
        app=app,
        summary=summary,
        details=normalized,
    )


def decode_timeline_response_object(response: str) -> dict[str, Any]:
    """解析模型返回的摘要对象，同时接受常见的 JSON 代码块包裹。"""
    raw = str(response or "").strip()
    if not raw:
        raise ValueError("timeline summary response is empty")
    fenced = _JSON_CODE_FENCE_RE.fullmatch(raw)
    if fenced is not None:
        raw = fenced.group("payload").strip()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("timeline summary response must be a JSON object")
    return data


def screenshot_uris_for_range(
    *,
    screenshots_dir: Path,
    start_time: str,
    end_time: str,
) -> list[str]:
    start = _parse_iso_time(start_time)
    end = _parse_iso_time(end_time)
    if end < start:
        return []
    root = Path(screenshots_dir)
    uris: list[str] = []
    for current_day in _days_between(start.date(), end.date()):
        day_dir = root / current_day.isoformat()
        if not day_dir.is_dir():
            continue
        for path in sorted(day_dir.iterdir()):
            timestamp = _parse_screenshot_timestamp(current_day, path.name)
            if timestamp is None:
                continue
            if timestamp < start or timestamp > end:
                continue
            uris.append(
                f"memory_space://timeline/screenshots/{current_day.isoformat()}/{path.name}"
            )
    return uris


def link_summary_screenshot(
    *,
    runtime_screenshot_path: Path,
    memory_screenshots_dir: Path,
    runtime_screenshots_dir: Path,
) -> Path:
    source = Path(runtime_screenshot_path).expanduser().resolve()
    runtime_root = Path(runtime_screenshots_dir).expanduser().resolve()
    try:
        relative = source.relative_to(runtime_root)
    except ValueError as error:
        raise ValueError("timeline screenshot source must be under runtime screenshots") from error
    target = Path(memory_screenshots_dir).expanduser().resolve() / relative
    ensure_dir(target.parent)
    if target.exists() or target.is_symlink():
        target.unlink()
    target.symlink_to(source)
    return target


def _summary_from_row(row: sqlite3.Row) -> TimelineSummaryRecord:
    details = json.loads(str(row["details"] or "[]"))
    return TimelineSummaryRecord(
        seq=int(row["seq"]),
        timeline_id=str(row["timeline_id"]),
        app_name=str(row["app_name"] or ""),
        start_time=str(row["start_time"]),
        end_time=str(row["end_time"]),
        summary=str(row["summary"]),
        details=[dict(item) for item in list(details or []) if isinstance(item, dict)],
        created_at=str(row["created_at"]),
    )


def _details_json(details: list[dict[str, Any]]) -> str:
    normalized = [
        {
            "start_time": str(item.get("start_time") or ""),
            "end_time": str(item.get("end_time") or ""),
            "description": None
            if item.get("description") is None
            else str(item.get("description") or ""),
            "capture_ids": [
                str(value)
                for value in list(item.get("capture_ids") or [])
                if str(value or "").strip()
            ],
        }
        for item in details
    ]
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True)


def _required_text(data: dict[str, Any], key: str) -> str:
    text = str(data.get(key) or "").strip()
    if not text:
        raise ValueError(f"timeline summary missing {key}")
    return text


def _validate_detail_start_times(
    start_times: list[str],
    *,
    final_end_time: str,
) -> None:
    if not start_times:
        return
    parsed = [_parse_timeline_output_time(value, field="start_time") for value in start_times]
    final = _parse_timeline_output_time(final_end_time, field="source_end_time")
    for index in range(len(parsed) - 1):
        if parsed[index] >= parsed[index + 1]:
            raise ValueError("timeline detail start_time must be strictly increasing")
    if parsed[-1] > final:
        raise ValueError("timeline detail start_time exceeds source_end_time")


def _parse_timeline_output_time(value: str, *, field: str) -> datetime:
    try:
        return _parse_iso_time(value)
    except ValueError as error:
        raise ValueError(f"timeline summary has invalid {field}") from error


def _parse_iso_time(value: str) -> datetime:
    return parse_user_time(value)


def _days_between(start: date, end: date) -> list[date]:
    days: list[date] = []
    current = start
    while current <= end:
        days.append(current)
        current = current + timedelta(days=1)
    return days


def _parse_screenshot_timestamp(day: date, filename: str) -> datetime | None:
    match = _SCREENSHOT_RE.match(filename)
    if match is None:
        return None
    return datetime.combine(
        day,
        time(
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second")),
            int(match.group("microsecond") or 0),
        ),
    )
