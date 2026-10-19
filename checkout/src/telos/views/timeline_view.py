"""Timeline 查询层：把 collector 的原始 session/capture 和 memory_space 的 LLM 摘要
合并成"单条纵向时间流"卡片。HTTP 层只跟这个 view 说话，不碰 sqlite。

设计取自行业调研(Dayflow/Timing/Rize)：一天是"一次只在一个 app"的顺序流，用一条
按时间排序的卡片流，而非 app 泳道。低于阈值的碎片合并：同 app 且间隔小 -> 并成一卡；
摘要作为卡片标题(headline)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from telos.core.clock import user_now
from telos.memory_space.timeline import (
    FAILED_SUMMARY,
    TimelineSummaryRecord,
    TimelineSummaryRepository,
)
from telos.timeline.grouping import group_timeline_sessions
from telos.timeline.repository import TimelineCapture, TimelineRepository, TimelineSession
from telos.views.app_icons import app_icon_for

_IGNORED_SESSION_SECONDS = 20.0


@dataclass(frozen=True)
class TimelineCard:
    kind: str
    app_name: str
    bundle_id: str
    window_title: str
    category: str            # 暂用 app_name（无类目体系）
    started_at: str
    ended_at: str
    duration_seconds: float
    headline: str
    summary: str
    has_summary: bool
    session_ids: list[str]
    primary_session_id: str
    timeline_id: str = ""  # 主 session 的摘要 id，供失败重试定位
    summary_status: str = ""
    failure_reason: str = ""


@dataclass(frozen=True)
class TimelineCaptureView:
    capture_id: str
    timestamp: str
    app_name: str
    bundle_id: str
    window_title: str
    url: str
    ax_text: str
    screenshot_path: str


@dataclass(frozen=True)
class TimelineSessionDetail:
    session_id: str
    app_name: str
    bundle_id: str
    window_title: str
    started_at: str
    ended_at: str
    duration_seconds: float
    summary: str
    status: str = ""
    summary_id: str = ""
    summary_status: str = ""
    details: list[dict[str, Any]] = field(default_factory=list)
    captures: list[TimelineCaptureView] = field(default_factory=list)


@dataclass(frozen=True)
class TimelineHit:
    capture_id: str
    session_id: str
    timestamp: str
    app_name: str
    bundle_id: str
    window_title: str
    snippet: str


@dataclass(frozen=True)
class TimelineAppStat:
    app_name: str
    bundle_id: str
    duration_seconds: float
    session_count: int


@dataclass(frozen=True)
class TimelineDayOverview:
    day: str
    duration_seconds: float
    session_count: int
    apps: list[TimelineAppStat] = field(default_factory=list)


@dataclass(frozen=True)
class TimelineDaySlice:
    kind: str
    label: str
    app_name: str
    bundle_id: str
    duration_seconds: float
    session_count: int = 0


@dataclass(frozen=True)
class TimelineDayBreakdown:
    day: str
    total_seconds: float
    slices: list[TimelineDaySlice] = field(default_factory=list)


def _parse_dt(value: str) -> datetime | None:
    text = str(value or "").strip().replace(" ", "T")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _span_seconds(start: datetime, end: datetime) -> float:
    try:
        return max(0.0, (end - start).total_seconds())
    except TypeError:
        try:
            return max(0.0, float(end.timestamp() - start.timestamp()))
        except (OSError, OverflowError, ValueError):
            return 0.0


def _summary_first_line(text: str) -> str:
    for line in str(text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


@dataclass(frozen=True)
class TimelineView:
    agent_dir: Path
    memory_space_dir: Path

    def list_days(self) -> list[str]:
        repo = TimelineRepository.for_agent_dir(self.agent_dir)
        try:
            return repo.list_session_days()
        finally:
            repo.close()

    def day(self, day: str) -> list[TimelineCard]:
        repo = TimelineRepository.for_agent_dir(self.agent_dir)
        try:
            sessions = repo.list_sessions_by_day(day)
        finally:
            repo.close()
        summaries = self._summaries_for_day(
            day,
            summary_ids={session.summary_id for session in sessions if session.summary_id},
        )
        failures = {
            timeline_id: _timeline_failure_info(self.memory_space_dir, timeline_id)
            for timeline_id, record in summaries.items()
            if record.summary == FAILED_SUMMARY
        }
        return _build_cards(sessions, summaries, failure_details=failures)

    def day_breakdown(self, day: str) -> TimelineDayBreakdown:
        repo = TimelineRepository.for_agent_dir(self.agent_dir)
        try:
            sessions = repo.list_sessions_by_day(day)
            captures = repo.list_captures_by_day(day)
        finally:
            repo.close()
        return _breakdown_for_day(day, sessions, captures)

    def overview(self, *, limit: int = 14) -> list[TimelineDayOverview]:
        repo = TimelineRepository.for_agent_dir(self.agent_dir)
        try:
            days = repo.list_session_days()[: max(1, int(limit or 14))]
            sessions_by_day = [(day, repo.list_sessions_by_day(day)) for day in days]
        finally:
            repo.close()
        return [_overview_for_day(day, sessions) for day, sessions in sessions_by_day]

    def app_icon(self, *, bundle_id: str = "", app_name: str = "") -> Path | None:
        return app_icon_for(
            cache_dir=self.agent_dir / "runtime" / "web" / "app_icons",
            bundle_id=bundle_id,
            app_name=app_name,
        )

    def session_detail(self, session_id: str) -> TimelineSessionDetail | None:
        repo = TimelineRepository.for_agent_dir(self.agent_dir)
        try:
            session = repo.get_session(session_id)
            if session is None:
                return None
            captures = repo.list_session_captures(session_id, include_duplicates=True)
        finally:
            repo.close()
        summary_record = self._summary_by_id(session.summary_id)
        summary_status = _session_summary_status(session, summary_record)
        return TimelineSessionDetail(
            session_id=session.id,
            app_name=session.app_name,
            bundle_id=session.bundle_id,
            window_title=session.window_title,
            started_at=session.started_at,
            ended_at=session.ended_at,
            duration_seconds=session.duration_seconds,
            summary=summary_record.summary if summary_record else "",
            status=session.status,
            summary_id=session.summary_id,
            summary_status=summary_status,
            details=list(summary_record.details) if summary_record else [],
            captures=[
                TimelineCaptureView(
                    capture_id=c.id,
                    timestamp=c.timestamp,
                    app_name=c.app_name,
                    bundle_id=c.bundle_id,
                    window_title=c.window_title,
                    url=c.url,
                    ax_text=c.ax_text,
                    screenshot_path=c.screenshot_path,
                )
                for c in captures
            ],
        )

    def search(self, query: str, *, limit: int = 50) -> list[TimelineHit]:
        repo = TimelineRepository.for_agent_dir(self.agent_dir)
        try:
            captures = repo.search_captures(query, limit=limit)
        finally:
            repo.close()
        hits: list[TimelineHit] = []
        for c in captures:
            hits.append(
                TimelineHit(
                    capture_id=c.id,
                    session_id=c.session_id,
                    timestamp=c.timestamp,
                    app_name=c.app_name,
                    bundle_id=c.bundle_id,
                    window_title=c.window_title,
                    snippet=_snippet(c.ax_text or c.window_title, query),
                )
            )
        return hits

    def _summaries_for_day(
        self,
        day: str,
        *,
        summary_ids: set[str] | None = None,
    ) -> dict[str, TimelineSummaryRecord]:
        repo = TimelineSummaryRepository.for_memory_space(self.memory_space_dir)
        try:
            records = repo.list(day=day)
            summaries = {rec.timeline_id: rec for rec in records}
            for timeline_id in sorted(summary_ids or set()):
                if timeline_id in summaries:
                    continue
                record = repo.get(timeline_id)
                if record is not None:
                    summaries[timeline_id] = record
        finally:
            repo.close()
        return summaries

    def _summary_by_id(self, timeline_id: str) -> TimelineSummaryRecord | None:
        if not timeline_id:
            return None
        repo = TimelineSummaryRepository.for_memory_space(self.memory_space_dir)
        try:
            return repo.get(timeline_id)
        finally:
            repo.close()


def _snippet(text: str, query: str, *, width: int = 160) -> str:
    raw = str(text or "").replace("\n", " ").strip()
    needle = str(query or "").strip().lower()
    if not raw:
        return ""
    idx = raw.lower().find(needle) if needle else -1
    if idx < 0:
        return raw[:width]
    start = max(0, idx - width // 3)
    return ("…" if start > 0 else "") + raw[start : start + width]


def _build_cards(
    sessions: list[TimelineSession],
    summaries: dict[str, TimelineSummaryRecord],
    *,
    failure_details: dict[str, str] | None = None,
) -> list[TimelineCard]:
    cards = [
        _card_from_cluster(
            cluster,
            summaries,
            failure_details=failure_details or {},
        )
        for cluster in group_timeline_sessions(sessions)
    ]
    return sorted(cards, key=lambda item: (item.started_at, item.primary_session_id), reverse=True)


def _card_from_cluster(
    cluster: list[TimelineSession],
    summaries: dict[str, TimelineSummaryRecord],
    *,
    failure_details: dict[str, str],
) -> TimelineCard:
    primary = max(cluster, key=lambda s: s.duration_seconds)
    total = sum(s.duration_seconds for s in cluster)
    started = min((s.started_at for s in cluster if s.started_at), default=primary.started_at)
    ended = max((s.ended_at for s in cluster if s.ended_at), default=primary.ended_at)
    primary_summary = summaries.get(primary.summary_id)
    has_summary = any(s.summary_id in summaries for s in cluster)
    summary_status = _cluster_summary_status(cluster, summaries)
    summary = _summary_for_cluster(cluster, summaries)

    kind = "session"
    app_name = primary.app_name or "未知应用"
    if primary_summary and primary_summary.summary:
        headline = _summary_first_line(primary_summary.summary)
    else:
        headline = primary.window_title or app_name

    timeline_id = _timeline_id_for_card(
        cluster,
        primary=primary,
        summary_status=summary_status,
    )
    failure_reason = failure_details.get(timeline_id, "")

    return TimelineCard(
        kind=kind,
        app_name=app_name,
        bundle_id=primary.bundle_id,
        window_title=primary.window_title,
        category=app_name,
        started_at=started,
        ended_at=ended,
        duration_seconds=float(total),
        headline=headline or app_name,
        summary=summary,
        has_summary=has_summary,
        session_ids=[s.id for s in cluster],
        primary_session_id=primary.id,
        timeline_id=timeline_id,
        summary_status=summary_status,
        failure_reason=failure_reason,
    )


def _timeline_id_for_card(
    cluster: list[TimelineSession],
    *,
    primary: TimelineSession,
    summary_status: str,
) -> str:
    if summary_status == "failed":
        for session in cluster:
            if str(session.status or "").strip().lower() == "failed" and session.summary_id:
                return str(session.summary_id)
    return str(primary.summary_id or "")


def _cluster_summary_status(
    cluster: list[TimelineSession],
    summaries: dict[str, TimelineSummaryRecord],
) -> str:
    statuses = [
        _session_summary_status(session, summaries.get(session.summary_id))
        for session in cluster
    ]
    if "failed" in statuses:
        return "failed"
    if "generating" in statuses:
        return "generating"
    if "queued" in statuses:
        return "queued"
    if "completed" in statuses:
        return "completed"
    return ""


def _session_summary_status(
    session: TimelineSession,
    summary: TimelineSummaryRecord | None,
) -> str:
    raw_status = str(session.status or "").strip().lower()
    if raw_status == "failed" or (summary is not None and summary.summary == FAILED_SUMMARY):
        return "failed"
    if raw_status == "processing":
        return "generating"
    if raw_status == "ready" or not str(session.summary_id or "").strip():
        return "queued"
    if summary is not None:
        return "completed"
    return "generating"


def _timeline_failure_info(memory_space_dir: Path, timeline_id: str) -> str:
    run_path = memory_space_dir / "timeline" / "runs" / timeline_id / "run.json"
    try:
        payload = json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "retry 0/0: 未记录具体错误"

    raw_error = str(payload.get("error") or "").strip()
    try:
        attempts = max(0, int(payload.get("attempt_count") or 0))
    except (TypeError, ValueError):
        attempts = 0
    return f"retry {attempts}/{attempts}: {raw_error or '未记录具体错误'}"


def _summary_for_cluster(
    cluster: list[TimelineSession],
    summaries: dict[str, TimelineSummaryRecord],
) -> str:
    texts: list[str] = []
    seen: set[str] = set()
    for session in cluster:
        summary_id = str(session.summary_id or "")
        if not summary_id or summary_id in seen:
            continue
        seen.add(summary_id)
        record = summaries.get(summary_id)
        text = str(record.summary or "").strip() if record else ""
        if text:
            texts.append(text)
    return "\n\n".join(texts)


def _accumulate_app_totals(
    sessions: list[TimelineSession], *, skip_zero: bool = False
) -> tuple[dict[tuple[str, str], dict[str, Any]], float]:
    """按 (app_name, bundle_id) 聚合时长与 session 数，返回 (totals, total_seconds)。

    skip_zero=True 时跳过零时长 session（既不累加时长也不计入 session_count）。
    """
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    total_seconds = 0.0
    for session in sessions:
        duration = max(0.0, float(session.duration_seconds or 0))
        if skip_zero and duration <= 0:
            continue
        total_seconds += duration
        app_name = session.app_name or "未知应用"
        bundle_id = session.bundle_id or ""
        current = totals.setdefault(
            (app_name, bundle_id),
            {
                "app_name": app_name,
                "bundle_id": bundle_id,
                "duration_seconds": 0.0,
                "session_count": 0,
            },
        )
        current["duration_seconds"] += duration
        current["session_count"] += 1
    return totals, total_seconds


def _overview_for_day(day: str, sessions: list[TimelineSession]) -> TimelineDayOverview:
    totals, total_seconds = _accumulate_app_totals(sessions)
    apps = [
        TimelineAppStat(
            app_name=str(item["app_name"]),
            bundle_id=str(item["bundle_id"]),
            duration_seconds=float(item["duration_seconds"]),
            session_count=int(item["session_count"]),
        )
        for item in totals.values()
    ]
    apps.sort(key=lambda item: (item.duration_seconds, item.session_count, item.app_name), reverse=True)
    return TimelineDayOverview(
        day=day,
        duration_seconds=float(total_seconds),
        session_count=len(sessions),
        apps=apps[:8],
    )


@dataclass(frozen=True)
class _BreakdownInterval:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class _CaptureGroup:
    key: str
    start: datetime
    end: datetime


def _breakdown_for_day(
    day: str,
    sessions: list[TimelineSession],
    captures: list[TimelineCapture],
    *,
    now: datetime | None = None,
) -> TimelineDayBreakdown:
    persisted_session_ids = {str(session.id) for session in sessions if str(session.id)}
    window = _day_window(day, now=now)

    app_totals, _ = _accumulate_app_totals(sessions, skip_zero=True)
    intervals: list[_BreakdownInterval] = []
    for session in sessions:
        if max(0.0, float(session.duration_seconds or 0)) <= 0:
            continue
        start = _parse_dt(session.started_at)
        end = _parse_dt(session.ended_at)
        if start is not None and end is not None and _span_seconds(start, end) > 0:
            intervals.append(_BreakdownInterval(start=start, end=end))

    ignored_seconds = 0.0
    ignored_count = 0
    capture_groups = _unpersisted_capture_groups(captures, persisted_session_ids)
    points = sorted([interval.start for interval in intervals] + [group.start for group in capture_groups])
    for group in capture_groups:
        next_start = _next_dt_after(points, group.start)
        end = next_start if next_start is not None else group.end
        duration = _span_seconds(group.start, end)
        if 0 < duration < _IGNORED_SESSION_SECONDS:
            ignored_seconds += duration
            ignored_count += 1
            intervals.append(_BreakdownInterval(start=group.start, end=end))

    app_seconds = sum(float(item["duration_seconds"]) for item in app_totals.values())
    if window is None:
        uncollected_seconds = _uncollected_seconds(intervals)
    else:
        window_seconds = _span_seconds(window.start, window.end)
        uncollected_seconds = max(0.0, window_seconds - app_seconds - ignored_seconds)
    slices = [
        TimelineDaySlice(
            kind="app",
            label=str(item["app_name"]),
            app_name=str(item["app_name"]),
            bundle_id=str(item["bundle_id"]),
            duration_seconds=float(item["duration_seconds"]),
            session_count=int(item["session_count"]),
        )
        for item in app_totals.values()
        if float(item["duration_seconds"]) > 0
    ]
    slices.sort(key=lambda item: (item.duration_seconds, item.session_count, item.label), reverse=True)
    if ignored_seconds > 0:
        slices.append(
            TimelineDaySlice(
                kind="ignored",
                label="忽略",
                app_name="忽略",
                bundle_id="",
                duration_seconds=ignored_seconds,
                session_count=ignored_count,
            )
        )
    if uncollected_seconds > 0:
        slices.append(
            TimelineDaySlice(
                kind="uncollected",
                label="未采集",
                app_name="未采集",
                bundle_id="",
                duration_seconds=uncollected_seconds,
                session_count=0,
            )
        )
    remaining_seconds = _remaining_day_seconds(day, now=now)
    if remaining_seconds > 0:
        slices.append(
            TimelineDaySlice(
                kind="remaining",
                label="剩余",
                app_name="剩余",
                bundle_id="",
                duration_seconds=remaining_seconds,
                session_count=0,
            )
        )
    return TimelineDayBreakdown(
        day=day,
        total_seconds=sum(item.duration_seconds for item in slices),
        slices=slices,
    )


def _unpersisted_capture_groups(
    captures: list[TimelineCapture],
    persisted_session_ids: set[str],
) -> list[_CaptureGroup]:
    grouped: dict[str, list[datetime]] = {}
    for capture in captures:
        session_id = str(capture.session_id or "")
        if session_id and session_id in persisted_session_ids:
            continue
        timestamp = _parse_dt(capture.timestamp)
        if timestamp is None:
            continue
        key = session_id or str(capture.id)
        grouped.setdefault(key, []).append(timestamp)

    groups: list[_CaptureGroup] = []
    for key, timestamps in grouped.items():
        ordered = sorted(timestamps)
        if not ordered:
            continue
        groups.append(_CaptureGroup(key=key, start=ordered[0], end=ordered[-1]))
    groups.sort(key=lambda item: (item.start, item.key))
    return groups


def _next_dt_after(points: list[datetime], current: datetime) -> datetime | None:
    for point in points:
        if _span_seconds(current, point) > 0:
            return point
    return None


def _day_start(day: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(day)).replace(hour=0, minute=0, second=0, microsecond=0)
    except ValueError:
        return None


def _day_window(day: str, *, now: datetime | None = None) -> _BreakdownInterval | None:
    start = _day_start(day)
    if start is None:
        return None
    current = now or user_now()
    end = current if start.date() == current.date() else start + timedelta(days=1)
    if _span_seconds(start, end) <= 0:
        return None
    return _BreakdownInterval(start=start, end=end)


def _remaining_day_seconds(day: str, *, now: datetime | None = None) -> float:
    start = _day_start(day)
    if start is None:
        return 0.0
    current = now or user_now()
    if start.date() != current.date():
        return 0.0
    return _span_seconds(current, start + timedelta(days=1))


def _uncollected_seconds(intervals: list[_BreakdownInterval]) -> float:
    ordered = sorted(intervals, key=lambda item: item.start)
    if not ordered:
        return 0.0
    total = 0.0
    cursor = ordered[0].end
    for interval in ordered[1:]:
        total += _span_seconds(cursor, interval.start)
        if _span_seconds(cursor, interval.end) > 0:
            cursor = interval.end
    return total
