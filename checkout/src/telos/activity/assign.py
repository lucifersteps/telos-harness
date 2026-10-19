"""快环路：把 timeline run details 归属到活动。

一次执行称为 activity assign run。只做归属判定，不做合并、拆分和定稿，那些在慢环路。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from telos.activity.repository import (
    MISC_ACTIVITY_ID,
    Activity,
    ActivityRepository,
    DetailAssignment,
)
from telos.activity.projection import sync_projection
from telos.activity.runner import (
    ActivityRetryExhausted,
    read_prompt_template,
    request_metadata,
    request_validated,
)
from telos.core.clock import parse_user_time, user_now
from telos.memory_space.timeline import (
    FAILED_SUMMARY,
    TimelineSummaryRecord,
    TimelineSummaryRepository,
    decode_timeline_response_object,
)
from telos.providers.base import ChatProvider, ChatRequest, Message
from telos.providers.registry import create_provider
from telos.storage.paths import default_source_root

logger = logging.getLogger(__name__)

DEFAULT_ASSIGN_PROMPT_PATH = default_source_root() / "profiles" / "activity_assign_prompt.md"

STATE_LAST_ASSIGNED_SEQ = "last_assigned_seq"
STATE_FAILED_RUNS = "failed_runs"

_NEW_ACTIVITY_ID = "new"
_MISC_POOL_ENTRY = (
    f"activity_id: {MISC_ACTIVITY_ID}\n"
    "title: 零散片段\n"
    "description: 短暂查看后离开、不构成独立事件的片段"
)


@dataclass(frozen=True)
class PendingDetail:
    """一条等待归属的 detail。detail_id 取它的第一个 capture_id。"""

    detail_id: str
    capture_ids: list[str]
    timeline_id: str
    timeline_seq: int
    detail_index: int
    start_time: str
    end_time: str
    app_name: str
    description: str


@dataclass(frozen=True)
class AssignGroup:
    activity_id: str
    detail_ids: list[str]
    title: str = ""
    description: str = ""


@dataclass(frozen=True)
class ActivityAssignResult:
    detail_count: int
    created_count: int
    sealed_count: int
    failed: bool = False
    failed_timeline_ids: list[str] = field(default_factory=list)


async def run_due_activity_assign(
    settings,
    *,
    provider: ChatProvider | None = None,
) -> int:
    """满足提交条件时消费一批 detail，返回本次归属的 detail 数。"""
    activity = settings.daemon.activity
    if not activity.enabled or not activity.assign.enabled:
        return 0
    with TimelineSummaryRepository.for_memory_space(
        settings.paths.memory_space_dir
    ) as summary_repository, ActivityRepository.for_agent_dir(
        settings.paths.agent_dir
    ) as activity_repository:
        watermark = int(activity_repository.get_state(STATE_LAST_ASSIGNED_SEQ, 0) or 0)
        runs = summary_repository.runs_after(seq=watermark)
        batch, consumed_seq = select_assign_batch(
            runs,
            window_seconds=activity.assign.window_seconds,
            max_details=activity.assign.max_details_per_run,
            now=user_now(),
        )
        if consumed_seq <= watermark:
            return 0
        if not batch:
            # 整段都是失败批次或缺少 capture_ids 的旧数据，只推进水位线。
            activity_repository.set_state(STATE_LAST_ASSIGNED_SEQ, consumed_seq)
            return 0
        result = await _assign_details(
            batch,
            settings=settings,
            activity_repository=activity_repository,
            provider=provider
            or create_provider(settings.providers, activity.assign.provider or None),
        )
        activity_repository.set_state(STATE_LAST_ASSIGNED_SEQ, consumed_seq)
        if result.failed_timeline_ids:
            _record_failed_runs(activity_repository, result.failed_timeline_ids)
        return result.detail_count


async def retry_failed_assign(
    settings,
    *,
    timeline_id: str,
    provider: ChatProvider | None = None,
) -> ActivityAssignResult:
    """手动重试一个失败批次。水位线已经越过它，因此按 timeline_id 定向取数。"""
    activity = settings.daemon.activity
    clean_id = str(timeline_id or "").strip()
    if not clean_id:
        raise ValueError("timeline_id is required")
    with TimelineSummaryRepository.for_memory_space(
        settings.paths.memory_space_dir
    ) as summary_repository, ActivityRepository.for_agent_dir(
        settings.paths.agent_dir
    ) as activity_repository:
        record = summary_repository.get(clean_id)
        if record is None:
            raise ValueError(f"unknown timeline run {clean_id!r}")
        batch = pending_details_for_run(record)
        if not batch:
            _clear_failed_run(activity_repository, clean_id)
            return ActivityAssignResult(detail_count=0, created_count=0, sealed_count=0)
        result = await _assign_details(
            batch,
            settings=settings,
            activity_repository=activity_repository,
            provider=provider
            or create_provider(settings.providers, activity.assign.provider or None),
        )
        if result.failed:
            _record_failed_runs(activity_repository, result.failed_timeline_ids)
        else:
            _clear_failed_run(activity_repository, clean_id)
        return result


async def _assign_details(
    batch: list[PendingDetail],
    *,
    settings,
    activity_repository: ActivityRepository,
    provider: ChatProvider,
) -> ActivityAssignResult:
    activity = settings.daemon.activity
    seal_idle_seconds = activity.seal_idle_seconds
    # run 合并会产生 seq 更大但时间更早的 run，因此取最值而不是取首尾。
    batch_start = min(_parse_time(detail.start_time) for detail in batch)
    batch_end = max(_parse_time(detail.end_time) for detail in batch)

    touched: list[str] = []
    sealed = _seal_idle_activities(
        activity_repository,
        reference_time=batch_start,
        seal_idle_seconds=seal_idle_seconds,
    )
    touched.extend(sealed)
    carried, fresh = _split_already_assigned(activity_repository, batch)
    if carried:
        _apply_carried_details(activity_repository, carried)
        touched.extend(activity_id for _, activity_id in carried)

    created = 0
    if fresh:
        overflow = _seal_pool_overflow(
            activity_repository,
            limit=activity.assign.pool_limit,
            reference_time=batch_start,
        )
        sealed.extend(overflow)
        touched.extend(overflow)
        pool = activity_repository.list_open_activities()
        try:
            activity_ids = [item.id for item in pool]
            detail_ids = [item.detail_id for item in fresh]
            groups = await request_validated(
                provider,
                request=_build_assign_request(settings, pool=pool, details=fresh),
                parse=lambda response: normalize_activity_assign_response(
                    response,
                    activity_ids=activity_ids,
                    detail_ids=detail_ids,
                ),
                retry_policy=activity.assign.retry,
                timeout_seconds=activity.assign.timeout_seconds,
                label="activity assign",
            )
        except ActivityRetryExhausted:
            logger.exception("activity assign failed after retries")
            _sync(activity_repository, settings=settings, activity_ids=touched)
            return ActivityAssignResult(
                detail_count=0,
                created_count=0,
                sealed_count=len(sealed),
                failed=True,
                failed_timeline_ids=sorted({detail.timeline_id for detail in fresh}),
            )
        created, applied = _apply_groups(
            activity_repository,
            groups=groups,
            details=fresh,
            seal_idle_seconds=seal_idle_seconds,
        )
        touched.extend(applied)

    idle = _seal_idle_activities(
        activity_repository,
        reference_time=batch_end,
        seal_idle_seconds=seal_idle_seconds,
    )
    sealed.extend(idle)
    touched.extend(idle)
    _sync(activity_repository, settings=settings, activity_ids=touched)
    return ActivityAssignResult(
        detail_count=len(batch),
        created_count=created,
        sealed_count=len(sealed),
    )


def _sync(
    repository: ActivityRepository,
    *,
    settings,
    activity_ids: list[str],
) -> None:
    sync_projection(
        repository,
        memory_space_dir=settings.paths.memory_space_dir,
        activity_ids=activity_ids,
    )


# ---- 取数 ---------------------------------------------------------------


def pending_details_for_run(record: TimelineSummaryRecord) -> list[PendingDetail]:
    """展开一个 timeline run 的 details。没有 capture_ids 的 detail 无法锚定，跳过。"""
    if str(record.summary or "").strip() == FAILED_SUMMARY:
        return []
    details: list[PendingDetail] = []
    for index, detail in enumerate(record.details):
        capture_ids = [
            str(value) for value in list(detail.get("capture_ids") or []) if str(value or "").strip()
        ]
        if not capture_ids:
            continue
        details.append(
            PendingDetail(
                detail_id=capture_ids[0],
                capture_ids=capture_ids,
                timeline_id=record.timeline_id,
                timeline_seq=record.seq,
                detail_index=index,
                start_time=str(detail.get("start_time") or record.start_time),
                end_time=str(detail.get("end_time") or record.end_time),
                app_name=record.app_name,
                description=str(detail.get("description") or ""),
            )
        )
    return details


def select_assign_batch(
    runs: list[TimelineSummaryRecord],
    *,
    window_seconds: int,
    max_details: int,
    now: datetime,
) -> tuple[list[PendingDetail], int]:
    """选出本次提交的 detail 和消费到的 seq。

    触发条件是最早一条未处理 detail 的 start 距当前时间超过 window_seconds。间隔的终点
    取当前时间，不取最后一条 detail 的 end。按 run 边界截取，不拆开单个 run。
    返回的 seq 会越过没有可归属 detail 的 run，避免水位线停滞。
    """
    batch: list[PendingDetail] = []
    consumed_seq = 0
    earliest: datetime | None = None
    for record in runs:
        details = pending_details_for_run(record)
        if not details:
            # 失败批次和缺少 capture_ids 的旧数据不进入快环路，但要推进水位线。
            if not batch:
                consumed_seq = record.seq
            continue
        if earliest is None:
            earliest = _parse_time(details[0].start_time)
            if (now - earliest).total_seconds() < max(1, int(window_seconds)):
                return [], consumed_seq
        if batch and len(batch) + len(details) > max(1, int(max_details)):
            break
        batch.extend(details)
        consumed_seq = record.seq
        if len(batch) >= max(1, int(max_details)):
            break
    if not batch:
        return [], consumed_seq
    return batch, consumed_seq


def _split_already_assigned(
    repository: ActivityRepository,
    batch: list[PendingDetail],
) -> tuple[list[tuple[PendingDetail, str]], list[PendingDetail]]:
    """重新送审的 run 会再次进入快环路，已有归属的 detail 由代码沿用，不再送模型。"""
    assignments = repository.assigned_activity_ids(
        [detail.detail_id for detail in batch],
    )
    carried: list[tuple[PendingDetail, str]] = []
    fresh: list[PendingDetail] = []
    for detail in batch:
        activity_id = assignments.get(detail.detail_id, "")
        if activity_id:
            carried.append((detail, activity_id))
        else:
            fresh.append(detail)
    return carried, fresh


def _apply_carried_details(
    repository: ActivityRepository,
    carried: list[tuple[PendingDetail, str]],
) -> None:
    repository.attach_details(
        [
            DetailAssignment(
                activity_id=activity_id,
                capture_ids=detail.capture_ids,
                timeline_id=detail.timeline_id,
                detail_index=detail.detail_index,
                start_time=detail.start_time,
                end_time=detail.end_time,
            )
            for detail, activity_id in carried
        ]
    )
    repository.recompute_spans([activity_id for _, activity_id in carried])


# ---- 活动池与关闭 -------------------------------------------------------


def _seal_pool_overflow(
    repository: ActivityRepository,
    *,
    limit: int,
    reference_time: datetime,
) -> list[str]:
    """活动池超过上限时关闭空闲最久的，避免归属准确率下降。返回被关闭的活动 id。"""
    pool = repository.list_open_activities()
    max_size = max(1, int(limit))
    if len(pool) <= max_size:
        return []
    overflow = sorted(pool, key=lambda item: item.span_end)[: len(pool) - max_size]
    for item in overflow:
        repository.seal_activity(
            item.id,
            sealed_at=item.span_end or reference_time.isoformat(timespec="seconds"),
            actor="fast",
        )
    return [item.id for item in overflow]


def _seal_idle_activities(
    repository: ActivityRepository,
    *,
    reference_time: datetime,
    seal_idle_seconds: int,
) -> list[str]:
    """关闭空闲超过 seal_idle_seconds 的活动，返回被关闭的活动 id。"""
    cutoff = reference_time - timedelta(seconds=max(1, int(seal_idle_seconds)))
    sealed: list[str] = []
    for item in repository.list_open_activities():
        if not item.span_end:
            continue
        try:
            span_end = parse_user_time(item.span_end)
        except ValueError:
            continue
        if span_end < cutoff:
            repository.seal_activity(
                item.id,
                sealed_at=span_end.isoformat(timespec="seconds"),
                actor="fast",
            )
            sealed.append(item.id)
    return sealed


# ---- 提示词 -------------------------------------------------------------


def render_activity_assign_input(
    *,
    pool: list[Activity],
    details: list[PendingDetail],
) -> str:
    """user 消息：活动池、分隔线、待归类片段。act_misc 固定拼在活动列表末尾。"""
    blocks = [
        "\n".join(
            [
                f"activity_id: {item.id}",
                f"title: {item.title}",
                f"description: {item.description}",
            ]
        )
        for item in pool
    ]
    blocks.append(_MISC_POOL_ENTRY)
    detail_blocks = [
        "\n".join(
            [
                f"detail_id: {detail.detail_id}",
                f"app: {detail.app_name or 'null'}",
                f"description: {detail.description}",
            ]
        )
        for detail in details
    ]
    return "\n\n".join(blocks) + "\n\n---\n\n" + "\n\n".join(detail_blocks)


def _build_assign_request(
    settings,
    *,
    pool: list[Activity],
    details: list[PendingDetail],
) -> ChatRequest:
    return ChatRequest(
        messages=[
            Message(
                role="user",
                content=render_activity_assign_input(pool=pool, details=details),
            )
        ],
        system_prompt=read_prompt_template(
            settings.daemon.activity.assign.prompt_template,
            default=DEFAULT_ASSIGN_PROMPT_PATH,
        ),
        metadata=request_metadata(settings, run_type="activity_assign"),
    )


# ---- 输出校验 -----------------------------------------------------------


def normalize_activity_assign_response(
    response: str,
    *,
    activity_ids: list[str],
    detail_ids: list[str],
) -> list[AssignGroup]:
    """任一约束不满足则拒绝整批，由调用方重试并把错误返回给模型。"""
    data = decode_timeline_response_object(response)
    raw_groups = data.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValueError("activity assign response must contain a non-empty groups array")
    known = set(activity_ids)
    expected = list(detail_ids)
    seen_details: set[str] = set()
    seen_activities: set[str] = set()
    groups: list[AssignGroup] = []
    for item in raw_groups:
        if not isinstance(item, dict):
            raise ValueError("activity assign group must be an object")
        activity_id = str(item.get("activity_id") or "").strip()
        if not activity_id:
            raise ValueError("activity assign group missing activity_id")
        if activity_id not in known and activity_id not in {
            _NEW_ACTIVITY_ID,
            MISC_ACTIVITY_ID,
        }:
            raise ValueError(f"unknown activity_id {activity_id!r}")
        if activity_id in known:
            if activity_id in seen_activities:
                raise ValueError(f"duplicate activity_id {activity_id!r}")
            seen_activities.add(activity_id)
        raw_ids = item.get("detail_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ValueError("activity assign group must contain detail_ids")
        clean_ids = [str(value) for value in raw_ids if str(value or "").strip()]
        if not clean_ids:
            raise ValueError("activity assign group must contain detail_ids")
        for value in clean_ids:
            if value not in expected:
                raise ValueError(f"unknown detail_id {value!r}")
            if value in seen_details:
                raise ValueError(f"duplicate detail_id {value!r}")
            seen_details.add(value)
        title = str(item.get("title") or "").strip()
        description = str(item.get("description") or "").strip()
        if activity_id == _NEW_ACTIVITY_ID and (not title or not description):
            raise ValueError("new activity requires title and description")
        if activity_id == MISC_ACTIVITY_ID:
            title = ""
            description = ""
        groups.append(
            AssignGroup(
                activity_id=activity_id,
                detail_ids=clean_ids,
                title=title,
                description=description,
            )
        )
    missing = [value for value in expected if value not in seen_details]
    if missing:
        raise ValueError(f"missing detail_id {missing[0]!r}")
    return groups


# ---- 落库 ---------------------------------------------------------------


def _apply_groups(
    repository: ActivityRepository,
    *,
    groups: list[AssignGroup],
    details: list[PendingDetail],
    seal_idle_seconds: int,
) -> tuple[int, list[str]]:
    """落库并返回新建的活动数与被改动的活动 id。"""
    by_id = {detail.detail_id: detail for detail in details}
    created = 0
    touched: list[str] = []
    for group in groups:
        members = [by_id[detail_id] for detail_id in group.detail_ids]
        members.sort(key=lambda item: item.start_time)
        if group.activity_id == MISC_ACTIVITY_ID:
            repository.attach_details(_assignments(MISC_ACTIVITY_ID, members))
            continue
        if group.activity_id == _NEW_ACTIVITY_ID:
            for chunk in _split_on_idle_gap(members, seal_idle_seconds=seal_idle_seconds):
                activity_id = repository.create_activity(
                    title=group.title,
                    description=group.description,
                )
                repository.attach_details(_assignments(activity_id, chunk))
                repository.append_revision(
                    activity_id=activity_id,
                    action="extend",
                    actor="fast",
                    payload={"detail_ids": [item.detail_id for item in chunk], "created": True},
                )
                touched.append(activity_id)
                created += 1
            continue
        previous = repository.get_activity(group.activity_id)
        repository.update_activity_text(
            group.activity_id,
            title=group.title,
            description=group.description,
        )
        repository.attach_details(_assignments(group.activity_id, members))
        payload: dict[str, object] = {"detail_ids": [item.detail_id for item in members]}
        if previous is not None and (group.title or group.description):
            payload["before"] = {
                "title": previous.title,
                "description": previous.description,
            }
        repository.append_revision(
            activity_id=group.activity_id,
            action="extend",
            actor="fast",
            payload=payload,
        )
        touched.append(group.activity_id)
    repository.recompute_spans(touched)
    return created, touched


def _assignments(activity_id: str, details: list[PendingDetail]) -> list[DetailAssignment]:
    return [
        DetailAssignment(
            activity_id=activity_id,
            capture_ids=detail.capture_ids,
            timeline_id=detail.timeline_id,
            detail_index=detail.detail_index,
            start_time=detail.start_time,
            end_time=detail.end_time,
        )
        for detail in details
    ]


def _split_on_idle_gap(
    details: list[PendingDetail],
    *,
    seal_idle_seconds: int,
) -> list[list[PendingDetail]]:
    """归入同一条新活动的片段，内部间隔超过 seal_idle 时由代码拆成两条活动。"""
    chunks: list[list[PendingDetail]] = []
    current: list[PendingDetail] = []
    limit = max(1, int(seal_idle_seconds))
    for detail in details:
        if current:
            gap = (
                _parse_time(detail.start_time) - _parse_time(current[-1].end_time)
            ).total_seconds()
            if gap > limit:
                chunks.append(current)
                current = []
        current.append(detail)
    if current:
        chunks.append(current)
    return chunks


def _record_failed_runs(repository: ActivityRepository, timeline_ids: list[str]) -> None:
    current = repository.get_state(STATE_FAILED_RUNS, []) or []
    merged = list(dict.fromkeys([*[str(item) for item in current], *timeline_ids]))
    repository.set_state(STATE_FAILED_RUNS, merged)


def _clear_failed_run(repository: ActivityRepository, timeline_id: str) -> None:
    current = repository.get_state(STATE_FAILED_RUNS, []) or []
    remaining = [str(item) for item in current if str(item) != str(timeline_id)]
    repository.set_state(STATE_FAILED_RUNS, remaining)


def _parse_time(value: str) -> datetime:
    return parse_user_time(str(value))
