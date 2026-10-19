"""把事实库的活动写成 memory space 投影。

事实库是 `<agent_dir>/runtime/activity.sqlite`，投影是
`<memory_space>/knowledge/knowledge.sqlite`。每次事实库改动收尾时按受影响的
activity_id 同步，不做全量重建。
"""

from __future__ import annotations

from pathlib import Path

from telos.activity.repository import (
    MISC_ACTIVITY_ID,
    STATUS_MERGED,
    ActivityRepository,
    detail_ids_for,
)
from telos.memory_space.activity import (
    ActivityProjectionRepository,
    ActivityProjectionRow,
)


def sync_projection(
    repository: ActivityRepository,
    *,
    memory_space_dir: Path | None,
    activity_ids: list[str],
) -> None:
    """同步给定活动。已合并、无片段和已删除的活动从投影移除。"""
    if memory_space_dir is None:
        return
    wanted = [
        activity_id
        for activity_id in dict.fromkeys(
            str(item) for item in activity_ids if str(item or "").strip()
        )
        if activity_id != MISC_ACTIVITY_ID
    ]
    if not wanted:
        return
    upserts: list[ActivityProjectionRow] = []
    deletes: list[str] = []
    for activity_id in wanted:
        activity = repository.get_activity(activity_id)
        if activity is None or activity.status == STATUS_MERGED or not activity.span_start:
            deletes.append(activity_id)
            continue
        upserts.append(
            ActivityProjectionRow(
                id=activity.id,
                status=activity.status,
                title=activity.title,
                description=activity.description,
                span_start=activity.span_start,
                span_end=activity.span_end,
                active_seconds=activity.active_seconds,
                detail_ids=detail_ids_for(repository.list_activity_captures(activity_id)),
            )
        )
    with ActivityProjectionRepository.for_memory_space(memory_space_dir) as projection:
        projection.delete(deletes)
        projection.upsert(upserts)
