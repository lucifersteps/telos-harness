"""慢环路：复核活动划分并写最终的标题和简介。

一次执行称为 activity consolidate run。只做合并、拆分、定稿三种操作。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from telos.activity.repository import (
    STATUS_OPEN,
    STATUS_SEALED,
    Activity,
    ActivityCapture,
    ActivityRepository,
    detail_ids_for,
)
from telos.activity.projection import sync_projection
from telos.activity.runner import (
    ActivityRetryExhausted,
    read_prompt_template,
    request_metadata,
    request_validated,
)
from telos.agent.context.template import render_prompt_template
from telos.core.clock import parse_user_time, user_now
from telos.memory_space.timeline import TimelineSummaryRepository, decode_timeline_response_object
from telos.providers.base import ChatProvider, ChatRequest, Message
from telos.providers.registry import create_provider
from telos.storage.paths import default_source_root

logger = logging.getLogger(__name__)

DEFAULT_CONSOLIDATE_PROMPT_PATH = (
    default_source_root() / "profiles" / "activity_consolidate_prompt.md"
)

STATE_LAST_CONSOLIDATED_AT = "last_consolidated_at"


@dataclass(frozen=True)
class MergePlan:
    activity_ids: list[str]
    title: str
    description: str


@dataclass(frozen=True)
class SplitSegment:
    detail_ids: list[str]
    title: str
    description: str


@dataclass(frozen=True)
class SplitPlan:
    activity_id: str
    segments: list[SplitSegment]


@dataclass(frozen=True)
class LabelPlan:
    activity_id: str
    title: str
    description: str


@dataclass(frozen=True)
class ConsolidatePlan:
    merges: list[MergePlan] = field(default_factory=list)
    splits: list[SplitPlan] = field(default_factory=list)
    labels: list[LabelPlan] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ActivityConsolidateResult:
    activity_count: int
    merged_count: int
    split_count: int
    labeled_count: int
    rejected: list[str] = field(default_factory=list)
    touched_ids: list[str] = field(default_factory=list)
    """本次被改动的活动 id，含被合并吸收的一方，供投影同步使用。"""


async def run_due_activity_consolidate(
    settings,
    *,
    provider: ChatProvider | None = None,
) -> int:
    """回看窗口内的活动做一次复核，返回被改动的活动数。"""
    activity = settings.daemon.activity
    if not activity.enabled or not activity.consolidate.enabled:
        return 0
    now = user_now()
    window_start = now - timedelta(seconds=max(1, int(activity.consolidate.lookback_seconds)))
    with ActivityRepository.for_agent_dir(settings.paths.agent_dir) as repository:
        activities = repository.list_activities_overlapping(
            start_time=window_start.isoformat(timespec="seconds"),
            end_time=now.isoformat(timespec="seconds"),
        )
        activities = [item for item in activities if item.span_start]
        if not activities:
            return 0
        captures = {
            item.id: repository.list_activity_captures(item.id) for item in activities
        }
        descriptions = _detail_descriptions(settings, captures)
        activity_ids = [item.id for item in activities]
        try:
            plan = await request_validated(
                provider
                or create_provider(settings.providers, activity.consolidate.provider or None),
                request=_build_consolidate_request(
                    settings,
                    activities=activities,
                    captures=captures,
                    descriptions=descriptions,
                ),
                parse=lambda response: normalize_activity_consolidate_response(
                    response,
                    activity_ids=activity_ids,
                ),
                retry_policy=activity.consolidate.retry,
                timeout_seconds=activity.consolidate.timeout_seconds,
                label="activity consolidate",
            )
        except ActivityRetryExhausted:
            logger.exception("activity consolidate failed after retries")
            return 0
        result = apply_consolidate_plan(
            repository,
            plan=plan,
            activities=activities,
            captures=captures,
            seal_idle_seconds=activity.seal_idle_seconds,
            edit_window_seconds=activity.consolidate.edit_window_seconds,
            now=now,
        )
        repository.set_state(STATE_LAST_CONSOLIDATED_AT, now.isoformat(timespec="seconds"))
        sync_projection(
            repository,
            memory_space_dir=settings.paths.memory_space_dir,
            activity_ids=result.touched_ids,
        )
        for reason in result.rejected:
            logger.warning("activity consolidate rejected a change | reason=%s", reason)
        return result.merged_count + result.split_count + result.labeled_count


# ---- 提示词 -------------------------------------------------------------


def _detail_descriptions(
    settings,
    captures: dict[str, list[ActivityCapture]],
) -> dict[str, tuple[str, str]]:
    """按 detail_id 取回应用名和描述。detail 内容只存在于 timeline_summaries。"""
    wanted: dict[str, set[int]] = {}
    for rows in captures.values():
        for row in rows:
            wanted.setdefault(row.timeline_id, set()).add(row.detail_index)
    result: dict[str, tuple[str, str]] = {}
    with TimelineSummaryRepository.for_memory_space(
        settings.paths.memory_space_dir
    ) as repository:
        for timeline_id, indexes in wanted.items():
            record = repository.get(timeline_id)
            if record is None:
                continue
            for index in indexes:
                if index >= len(record.details):
                    continue
                detail = record.details[index]
                capture_ids = [
                    str(value)
                    for value in list(detail.get("capture_ids") or [])
                    if str(value or "").strip()
                ]
                if not capture_ids:
                    continue
                result[capture_ids[0]] = (
                    record.app_name,
                    str(detail.get("description") or ""),
                )
    return result


def render_activity_consolidate_input(
    *,
    activities: list[Activity],
    captures: dict[str, list[ActivityCapture]],
    descriptions: dict[str, tuple[str, str]],
) -> str:
    blocks: list[str] = []
    for item in activities:
        lines = [
            f"activity_id: {item.id}",
            f"status: {item.status}",
            f"title: {item.title}",
            f"description: {item.description}",
            "details:",
        ]
        for detail_id in detail_ids_for(captures.get(item.id, [])):
            app_name, description = descriptions.get(detail_id, ("", ""))
            lines.append(f"  {detail_id} | {app_name or 'null'} | {description}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _build_consolidate_request(
    settings,
    *,
    activities: list[Activity],
    captures: dict[str, list[ActivityCapture]],
    descriptions: dict[str, tuple[str, str]],
) -> ChatRequest:
    template = read_prompt_template(
        settings.daemon.activity.consolidate.prompt_template,
        default=DEFAULT_CONSOLIDATE_PROMPT_PATH,
    )
    system_prompt = render_prompt_template(
        template,
        {"memory": _memory_content(settings.paths.memory_space_dir)},
        section="activity.consolidate",
    ).strip()
    return ChatRequest(
        messages=[
            Message(
                role="user",
                content=render_activity_consolidate_input(
                    activities=activities,
                    captures=captures,
                    descriptions=descriptions,
                ),
            )
        ],
        system_prompt=system_prompt,
        metadata=request_metadata(settings, run_type="activity_consolidate"),
    )


def _memory_content(memory_space_dir: Path | None) -> str:
    if memory_space_dir is None:
        return "(memory space is not configured)"
    path = Path(memory_space_dir) / "memory" / "memory.md"
    if not path.is_file():
        return "(memory.md not found)"
    return path.read_text(encoding="utf-8").strip() or "(empty)"


# ---- 输出校验 -----------------------------------------------------------


def normalize_activity_consolidate_response(
    response: str,
    *,
    activity_ids: list[str],
) -> ConsolidatePlan:
    """解析模型输出。格式错误抛 ValueError 触发重试；同一个 activity_id 出现在
    多个操作中时只跳过冲突的那条，其余操作保留。"""
    data = decode_timeline_response_object(response)
    known = set(activity_ids)
    used: set[str] = set()
    rejected: list[str] = []

    merges: list[MergePlan] = []
    for item in _object_list(data, "merges"):
        ids = [str(value) for value in list(item.get("activity_ids") or []) if str(value or "")]
        if len(ids) < 2:
            raise ValueError("merge requires at least two activity_ids")
        if not _claim(ids, known=known, used=used, rejected=rejected, operation="merge"):
            continue
        title = str(item.get("title") or "").strip()
        description = str(item.get("description") or "").strip()
        if not title:
            raise ValueError("merge requires title")
        merges.append(MergePlan(activity_ids=ids, title=title, description=description))

    splits: list[SplitPlan] = []
    for item in _object_list(data, "splits"):
        activity_id = str(item.get("activity_id") or "").strip()
        if not _claim([activity_id], known=known, used=used, rejected=rejected, operation="split"):
            continue
        raw_segments = item.get("segments")
        if not isinstance(raw_segments, list) or len(raw_segments) < 2:
            raise ValueError("split requires at least two segments")
        segments: list[SplitSegment] = []
        for raw in raw_segments:
            if not isinstance(raw, dict):
                raise ValueError("split segment must be an object")
            detail_ids = [
                str(value)
                for value in list(raw.get("detail_ids") or [])
                if str(value or "").strip()
            ]
            if not detail_ids:
                raise ValueError("split segment requires detail_ids")
            title = str(raw.get("title") or "").strip()
            if not title:
                raise ValueError("split segment missing title")
            segments.append(
                SplitSegment(
                    detail_ids=detail_ids,
                    title=title,
                    description=str(raw.get("description") or "").strip(),
                )
            )
        splits.append(SplitPlan(activity_id=activity_id, segments=segments))

    labels: list[LabelPlan] = []
    for item in _object_list(data, "labels"):
        activity_id = str(item.get("activity_id") or "").strip()
        if not _claim([activity_id], known=known, used=used, rejected=rejected, operation="label"):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            raise ValueError("label requires title")
        labels.append(
            LabelPlan(
                activity_id=activity_id,
                title=title,
                description=str(item.get("description") or "").strip(),
            )
        )
    return ConsolidatePlan(merges=merges, splits=splits, labels=labels, rejected=rejected)


def _object_list(data: dict, key: str) -> list[dict]:
    raw = data.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{key} must be an array")
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"{key} entries must be objects")
    return list(raw)


def _claim(
    activity_ids: list[str],
    *,
    known: set[str],
    used: set[str],
    rejected: list[str],
    operation: str,
) -> bool:
    for activity_id in activity_ids:
        if activity_id not in known:
            raise ValueError(f"unknown activity_id {activity_id!r}")
        if activity_id in used:
            rejected.append(
                f"{operation} skipped: activity_id {activity_id!r} appears in more than one operation"
            )
            return False
    used.update(activity_ids)
    return True


# ---- 落库与校验 ---------------------------------------------------------


def apply_consolidate_plan(
    repository: ActivityRepository,
    *,
    plan: ConsolidatePlan,
    activities: list[Activity],
    captures: dict[str, list[ActivityCapture]],
    seal_idle_seconds: int,
    edit_window_seconds: int,
    now: datetime,
) -> ActivityConsolidateResult:
    """逐条校验后落库。任一校验不通过则拒绝该条变更，其余变更照常执行。"""
    by_id = {item.id: item for item in activities}
    captures = {key: list(value) for key, value in captures.items()}
    rejected: list[str] = list(plan.rejected)
    touched: list[str] = []
    merged = 0
    split = 0
    labeled = 0

    for merge in plan.merges:
        reason = _reject_merge(
            merge,
            by_id=by_id,
            captures=captures,
            seal_idle_seconds=seal_idle_seconds,
            edit_window_seconds=edit_window_seconds,
            now=now,
        )
        if reason:
            rejected.append(reason)
            continue
        keep_id = min(merge.activity_ids, key=lambda item: by_id[item].span_start)
        absorbed = [item for item in merge.activity_ids if item != keep_id]
        repository.merge_activities(keep_id=keep_id, merged_ids=absorbed, actor="slow")
        _relabel(
            repository,
            activity=by_id[keep_id],
            title=merge.title,
            description=merge.description,
        )
        # 同步本地快照：片段已迁到保留的活动，后续合并的间隔判定要看到迁移后的状态。
        for activity_id in absorbed:
            captures[keep_id] = captures.get(keep_id, []) + captures.pop(activity_id, [])
        survivor = repository.get_activity(keep_id)
        if survivor is not None:
            by_id[keep_id] = survivor
        touched.append(keep_id)
        touched.extend(absorbed)
        merged += 1

    for item in plan.splits:
        reason = _reject_split(
            item,
            by_id=by_id,
            captures=captures,
            edit_window_seconds=edit_window_seconds,
            now=now,
        )
        if reason:
            rejected.append(reason)
            continue
        touched.extend(
            repository.split_activity(
                activity_id=item.activity_id,
                segments=[
                    {
                        "detail_ids": segment.detail_ids,
                        "title": segment.title,
                        "description": segment.description,
                    }
                    for segment in item.segments
                ],
                actor="slow",
            )
        )
        split += 1

    for label in plan.labels:
        reason = _reject_edit_window(
            by_id.get(label.activity_id),
            edit_window_seconds=edit_window_seconds,
            now=now,
        )
        if reason:
            rejected.append(reason)
            continue
        _relabel(
            repository,
            activity=by_id[label.activity_id],
            title=label.title,
            description=label.description,
        )
        touched.append(label.activity_id)
        labeled += 1

    return ActivityConsolidateResult(
        activity_count=len(activities),
        merged_count=merged,
        split_count=split,
        labeled_count=labeled,
        rejected=rejected,
        touched_ids=list(dict.fromkeys(touched)),
    )


def _relabel(
    repository: ActivityRepository,
    *,
    activity: Activity,
    title: str,
    description: str,
) -> None:
    """写最终文案，revision 记录改动前后的值以便撤销。"""
    repository.update_activity_text(activity.id, title=title, description=description)
    repository.append_revision(
        activity_id=activity.id,
        action="relabel",
        actor="slow",
        payload={
            "before": {"title": activity.title, "description": activity.description},
            "after": {"title": title, "description": description},
        },
    )


def _reject_merge(
    merge: MergePlan,
    *,
    by_id: dict[str, Activity],
    captures: dict[str, list[ActivityCapture]],
    seal_idle_seconds: int,
    edit_window_seconds: int,
    now: datetime,
) -> str:
    for activity_id in merge.activity_ids:
        reason = _reject_edit_window(
            by_id.get(activity_id),
            edit_window_seconds=edit_window_seconds,
            now=now,
        )
        if reason:
            return reason
    intervals: list[tuple[datetime, datetime]] = []
    for activity_id in merge.activity_ids:
        for capture in captures.get(activity_id, []):
            try:
                intervals.append(
                    (parse_user_time(capture.start_time), parse_user_time(capture.end_time))
                )
            except ValueError:
                return f"merge {activity_id} has an unparseable capture interval"
    if not intervals:
        return f"merge {merge.activity_ids} has no captures"
    intervals.sort()
    limit = max(1, int(seal_idle_seconds))
    reach = intervals[0][1]
    for start, end in intervals[1:]:
        if (start - reach).total_seconds() > limit:
            return f"merge {merge.activity_ids} exceeds seal_idle"
        reach = max(reach, end)
    return ""


def _reject_split(
    plan: SplitPlan,
    *,
    by_id: dict[str, Activity],
    captures: dict[str, list[ActivityCapture]],
    edit_window_seconds: int,
    now: datetime,
) -> str:
    reason = _reject_edit_window(
        by_id.get(plan.activity_id),
        edit_window_seconds=edit_window_seconds,
        now=now,
    )
    if reason:
        return reason
    ordered = detail_ids_for(captures.get(plan.activity_id, []))
    if not ordered:
        return f"split {plan.activity_id} has no captures"
    known = set(ordered)
    seen: set[str] = set()
    for segment in plan.segments:
        for detail_id in segment.detail_ids:
            if detail_id not in known:
                return f"split {plan.activity_id} references unknown detail_id"
            if detail_id in seen:
                return f"split {plan.activity_id} assigns detail_id to more than one segment"
            seen.add(detail_id)
    if seen != known:
        return f"split {plan.activity_id} must cover every detail of the activity"
    return ""


def _reject_edit_window(
    activity: Activity | None,
    *,
    edit_window_seconds: int,
    now: datetime,
) -> str:
    if activity is None:
        return "unknown activity"
    if activity.status not in {STATUS_OPEN, STATUS_SEALED}:
        return f"activity {activity.id} is not editable"
    if not activity.sealed_at:
        return ""
    try:
        sealed_at = parse_user_time(activity.sealed_at)
    except ValueError:
        return f"activity {activity.id} has an unparseable sealed_at"
    if (now - sealed_at).total_seconds() > max(1, int(edit_window_seconds)):
        return f"activity {activity.id} is outside the edit window"
    return ""
