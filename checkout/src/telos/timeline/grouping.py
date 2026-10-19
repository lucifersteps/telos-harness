"""时间线会话的统一分组规则。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, TypeVar

from telos.core.clock import parse_user_time


TIMELINE_GROUP_GAP_SECONDS = 180.0

_SessionT = TypeVar("_SessionT")


def timeline_session_app_key(session: Any) -> str:
    """返回稳定的程序标识；缺少包标识时退回程序名称。"""
    bundle_id = str(getattr(session, "bundle_id", "") or "").strip()
    if bundle_id:
        return f"bundle:{bundle_id}"
    app_name = str(getattr(session, "app_name", "") or "").strip()
    return f"name:{app_name}" if app_name else ""


def timeline_sessions_share_group(previous: Any, current: Any) -> bool:
    """判断按时间相邻的两个会话是否属于同一活动组。"""
    previous_key = timeline_session_app_key(previous)
    if not previous_key or previous_key != timeline_session_app_key(current):
        return False
    gap = _timeline_session_gap_seconds(previous, current)
    return gap is not None and gap <= TIMELINE_GROUP_GAP_SECONDS


def group_timeline_sessions(sessions: Iterable[_SessionT]) -> list[list[_SessionT]]:
    """按时间排序，并用统一规则把任意数量的相邻会话分组。"""
    ordered = sorted(
        sessions,
        key=lambda item: (
            str(getattr(item, "started_at", "") or ""),
            int(getattr(item, "seq", 0) or 0),
        ),
    )
    groups: list[list[_SessionT]] = []
    for session in ordered:
        if groups and timeline_sessions_share_group(groups[-1][-1], session):
            groups[-1].append(session)
        else:
            groups.append([session])
    return groups


def timeline_session_group(
    sessions: Iterable[_SessionT],
    *,
    session_ids: Iterable[str],
) -> list[_SessionT]:
    """返回包含指定会话的完整组；指定会话跨组时拒绝处理。"""
    targets = {
        str(item or "").strip()
        for item in session_ids
        if str(item or "").strip()
    }
    if not targets:
        return []
    matched = [
        group
        for group in group_timeline_sessions(sessions)
        if targets.intersection(str(getattr(item, "id", "") or "") for item in group)
    ]
    if len(matched) != 1:
        raise ValueError("指定的时间线会话不属于同一个活动组")
    group = matched[0]
    found = {str(getattr(item, "id", "") or "") for item in group}
    if not targets.issubset(found):
        raise ValueError("时间线活动组缺少指定会话")
    return group


def _timeline_session_gap_seconds(previous: Any, current: Any) -> float | None:
    previous_end = str(getattr(previous, "ended_at", "") or "").strip()
    current_start = str(getattr(current, "started_at", "") or "").strip()
    if not previous_end or not current_start:
        return None
    try:
        return max(
            0.0,
            (parse_user_time(current_start) - parse_user_time(previous_end)).total_seconds(),
        )
    except ValueError:
        return None
