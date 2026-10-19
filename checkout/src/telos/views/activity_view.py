"""Activity 查询层：把活动、片段和变更记录整理成活动道需要的形状。

活动道是回顾页「计划对比」里与 Todos 并排的那条道，是活动图谱唯一的展示位置。things
页面的日历与本模块无关。

HTTP 层只跟这个 view 说话，不直接访问 sqlite。主副辅三档的划分在这里完成，规则见
`assign_tiers`。

读的是智能体目录下的事实库 `runtime/activity.sqlite`，因此入口参数是 `agent_dir`。
memory space 的活动投影只服务模型，不参与这里的查询；取 detail 描述时另需
`memory_space_dir` 打开 `TimelineSummaryRepository`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from telos.activity.projection import sync_projection
from telos.activity.repository import (
    STATUS_OPEN,
    STATUS_SEALED,
    Activity,
    ActivityCapture,
    ActivityRepository,
    detail_ids_for,
)
from telos.core.clock import parse_user_time
from telos.memory_space.timeline import TimelineSummaryRepository
from telos.timeline.repository import TimelineRepository

TIER_MAIN = "main"
TIER_SECONDARY = "secondary"
TIER_MINOR = "minor"

DEFAULT_MIN_UNCOVERED_SECONDS = 1200

UNDOABLE_ACTIONS = frozenset({"merge", "split", "relabel"})
"""可撤销的变更。extend 撤销后片段会失去归属，seal 是终态，两者都不提供撤销。"""


@dataclass(frozen=True)
class ActivityBlock:
    activity_id: str
    status: str
    title: str
    description: str
    span_start: str
    span_end: str
    active_seconds: float
    density: float
    tier: str
    detail_count: int


@dataclass(frozen=True)
class ActivityDetailView:
    detail_id: str
    timeline_id: str
    start_time: str
    end_time: str
    app_name: str
    description: str
    capture_ids: list[str] = field(default_factory=list)
    screenshot_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ActivityRevisionView:
    revision_id: str
    action: str
    actor: str
    created_at: str
    payload: dict
    undoable: bool


@dataclass(frozen=True)
class ActivityDetailPage:
    activity_id: str
    status: str
    title: str
    description: str
    span_start: str
    span_end: str
    active_seconds: float
    density: float
    superseded_by: str
    split_from: str
    sealed_at: str
    details: list[ActivityDetailView] = field(default_factory=list)
    revisions: list[ActivityRevisionView] = field(default_factory=list)


@dataclass(frozen=True)
class ActivityDay:
    day: str
    blocks: list[ActivityBlock] = field(default_factory=list)


def activity_day(
    agent_dir: Path,
    day: str,
    *,
    min_uncovered_seconds: int = DEFAULT_MIN_UNCOVERED_SECONDS,
) -> ActivityDay:
    """某一天的活动块。跨日活动只要与当天相交就会出现在当天。"""
    start, end = _day_bounds(day)
    with ActivityRepository.for_agent_dir(agent_dir) as repository:
        activities = [
            item
            for item in repository.list_activities_overlapping(
                start_time=start.isoformat(timespec="seconds"),
                end_time=end.isoformat(timespec="seconds"),
                statuses=(STATUS_OPEN, STATUS_SEALED),
            )
            if item.span_start
        ]
        detail_counts = {
            item.id: len(detail_ids_for(repository.list_activity_captures(item.id)))
            for item in activities
        }
    return ActivityDay(
        day=str(day),
        blocks=activity_blocks(
            activities,
            detail_counts,
            min_uncovered_seconds=min_uncovered_seconds,
        ),
    )


def assign_tiers(
    activities: list[Activity],
    *,
    min_uncovered_seconds: int,
) -> dict[str, tuple[str, list[tuple[datetime, datetime]]]]:
    """把活动分成主、副、辅三档，并给出主线和副线各自占住的时段。

    同一条规则跑两遍。每一遍按跨度从长到短过一遍候选：一条活动的跨度里还有多少时间
    没被本遍已选中的活动占住，这个时长超过 `min_uncovered_seconds` 就选中它，它占住的
    就是那段没被占住的时间。第一遍在全部活动里选，结果是主线；第二遍把第一遍没选中的
    清空重来再选一次，结果是副线；两遍都没选中的是辅。

    门槛必须大于零。取零时跨度完全被覆盖的活动也会被选中，一天里绝大多数活动都会变成
    主线。
    """
    limit = max(1, int(min_uncovered_seconds))
    main, rest = _greedy_cover(activities, limit)
    secondary, minor = _greedy_cover(rest, limit)
    tiers: dict[str, tuple[str, list[tuple[datetime, datetime]]]] = {}
    for item, claimed in main:
        tiers[item.id] = (TIER_MAIN, claimed)
    for item, claimed in secondary:
        tiers[item.id] = (TIER_SECONDARY, claimed)
    for item in minor:
        tiers[item.id] = (TIER_MINOR, [])
    return tiers


def activity_blocks(
    activities: list[Activity],
    detail_counts: dict[str, int],
    *,
    min_uncovered_seconds: int = DEFAULT_MIN_UNCOVERED_SECONDS,
) -> list[ActivityBlock]:
    tiers = assign_tiers(activities, min_uncovered_seconds=min_uncovered_seconds)
    blocks = [
        ActivityBlock(
            activity_id=item.id,
            status=item.status,
            title=item.title,
            description=item.description,
            span_start=item.span_start,
            span_end=item.span_end,
            active_seconds=item.active_seconds,
            density=item.density,
            tier=tiers[item.id][0],
            detail_count=int(detail_counts.get(item.id, 0)),
        )
        for item in activities
        if item.id in tiers
    ]
    blocks.sort(key=lambda item: (item.span_start, item.activity_id))
    return blocks


def _greedy_cover(
    activities: list[Activity],
    min_uncovered_seconds: int,
) -> tuple[list[tuple[Activity, list[tuple[datetime, datetime]]]], list[Activity]]:
    picked: list[tuple[Activity, list[tuple[datetime, datetime]]]] = []
    taken: list[tuple[datetime, datetime]] = []
    rest: list[Activity] = []
    ordered = sorted(
        (item for item in activities if _span(item) is not None),
        key=lambda item: (-_span_seconds(item), item.span_start, item.id),
    )
    for item in ordered:
        claimed = _subtract([_span(item)], taken)
        free = sum((end - start).total_seconds() for start, end in claimed)
        if free >= min_uncovered_seconds and free > 0:
            picked.append((item, claimed))
            taken.append(_span(item))
        else:
            rest.append(item)
    return picked, rest


def _subtract(
    spans: list[tuple[datetime, datetime]],
    cuts: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    result = list(spans)
    for cut_start, cut_end in cuts:
        remaining: list[tuple[datetime, datetime]] = []
        for start, end in result:
            if cut_end <= start or cut_start >= end:
                remaining.append((start, end))
                continue
            if cut_start > start:
                remaining.append((start, cut_start))
            if cut_end < end:
                remaining.append((cut_end, end))
        result = remaining
    return result


def _span(activity: Activity) -> tuple[datetime, datetime] | None:
    if not activity.span_start or not activity.span_end:
        return None
    try:
        return parse_user_time(activity.span_start), parse_user_time(activity.span_end)
    except ValueError:
        return None


def _span_seconds(activity: Activity) -> float:
    span = _span(activity)
    return 0.0 if span is None else (span[1] - span[0]).total_seconds()


def activity_detail(
    agent_dir: Path,
    activity_id: str,
    *,
    memory_space_dir: Path | None = None,
) -> ActivityDetailPage | None:
    with ActivityRepository.for_agent_dir(agent_dir) as repository:
        activity = repository.get_activity(activity_id)
        if activity is None:
            return None
        captures = repository.list_activity_captures(activity_id)
        revisions = [
            ActivityRevisionView(
                revision_id=item.id,
                action=item.action,
                actor=item.actor,
                created_at=item.created_at,
                payload=item.payload,
                undoable=item.action in UNDOABLE_ACTIONS,
            )
            for item in repository.list_revisions(activity_id)
        ]
    return ActivityDetailPage(
        activity_id=activity.id,
        status=activity.status,
        title=activity.title,
        description=activity.description,
        span_start=activity.span_start,
        span_end=activity.span_end,
        active_seconds=activity.active_seconds,
        density=activity.density,
        superseded_by=activity.superseded_by,
        split_from=activity.split_from,
        sealed_at=activity.sealed_at,
        details=_detail_views(memory_space_dir, captures, agent_dir=agent_dir),
        revisions=revisions,
    )


def undo_activity_revision(
    agent_dir: Path,
    revision_id: str,
    *,
    memory_space_dir: Path | None = None,
) -> str:
    """撤销一次变更，返回被撤销的 action。"""
    with ActivityRepository.for_agent_dir(agent_dir) as repository:
        revision = repository.get_revision(revision_id)
        touched = _undo_touched_ids(repository, revision) if revision is not None else []
        action = repository.undo_revision(revision_id)
        sync_projection(
            repository,
            memory_space_dir=memory_space_dir,
            activity_ids=touched,
        )
        return action


def _undo_touched_ids(repository: ActivityRepository, revision) -> list[str]:
    """撤销一条变更会改动哪些活动。撤销前采集，因为拆分产生的段会被删除。"""
    touched = [revision.activity_id]
    if revision.action == "merge":
        touched.extend(
            str(item.get("activity_id") or "")
            for item in list(revision.payload.get("merged") or [])
        )
    elif revision.action == "split":
        touched.extend(str(item) for item in list(revision.payload.get("activity_ids") or []))
    return [item for item in dict.fromkeys(touched) if item]


def _detail_views(
    memory_space_dir: Path | None,
    captures: list[ActivityCapture],
    *,
    agent_dir: Path | None,
) -> list[ActivityDetailView]:
    by_detail: dict[tuple[str, int], list[ActivityCapture]] = {}
    for capture in captures:
        by_detail.setdefault((capture.timeline_id, capture.detail_index), []).append(capture)
    views: list[ActivityDetailView] = []
    records = {}
    if memory_space_dir is not None:
        with TimelineSummaryRepository.for_memory_space(memory_space_dir) as repository:
            records = {
                timeline_id: repository.get(timeline_id)
                for timeline_id in {key[0] for key in by_detail}
            }
    screenshots = _screenshot_paths(
        agent_dir,
        capture_ids=[capture.capture_id for capture in captures],
    )
    for (timeline_id, detail_index), rows in by_detail.items():
        record = records.get(timeline_id)
        description = ""
        app_name = ""
        if record is not None and detail_index < len(record.details):
            description = str(record.details[detail_index].get("description") or "")
            app_name = record.app_name
        views.append(
            ActivityDetailView(
                detail_id=rows[0].capture_id,
                timeline_id=timeline_id,
                start_time=rows[0].start_time,
                end_time=rows[0].end_time,
                app_name=app_name,
                description=description,
                capture_ids=[row.capture_id for row in rows],
                screenshot_paths=[
                    path
                    for row in rows
                    if (path := screenshots.get(row.capture_id, ""))
                ],
            )
        )
    views.sort(key=lambda item: (item.start_time, item.detail_id))
    return views


def _screenshot_paths(agent_dir: Path | None, *, capture_ids: list[str]) -> dict[str, str]:
    """capture_id 到截图路径。原始采集在智能体目录，不在 memory space。"""
    if agent_dir is None or not capture_ids:
        return {}
    paths: dict[str, str] = {}
    with TimelineRepository.for_agent_dir(agent_dir) as repository:
        for capture_id in capture_ids:
            capture = repository.get_capture(capture_id)
            if capture is not None and capture.screenshot_path:
                paths[capture_id] = capture.screenshot_path
    return paths


def _day_bounds(day: str) -> tuple[datetime, datetime]:
    start = parse_user_time(f"{str(day).strip()}T00:00:00")
    return start, start + timedelta(days=1) - timedelta(seconds=1)
