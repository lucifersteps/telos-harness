"""首页概览查询：组合事项与屏幕活动的只读投影。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from telos.core.clock import ensure_user_time
from telos.views.things_view import ThingsOverview


@dataclass(frozen=True)
class OverviewAppActivity:
    app_name: str
    bundle_id: str
    duration_seconds: float


@dataclass(frozen=True)
class OverviewActivity:
    available: bool
    today_seconds: float = 0
    week_seconds: float = 0
    today_apps: list[OverviewAppActivity] = field(default_factory=list)


@dataclass(frozen=True)
class OverviewThings:
    available: bool
    open: int = 0
    scheduled_today: int = 0
    overdue: int = 0
    deadline_passed: int = 0
    completed_today: int = 0


@dataclass(frozen=True)
class OverviewSnapshot:
    date: str
    things: OverviewThings
    activity: OverviewActivity


@dataclass(frozen=True)
class OverviewView:
    things_view: Any | None
    timeline_view: Any | None

    def snapshot(self, *, now: datetime | None = None) -> OverviewSnapshot:
        current = ensure_user_time(now)
        today = current.date()
        day = today.isoformat()
        things = self._things(day)
        activity = self._activity(day, (today - timedelta(days=today.weekday())).isoformat())
        return OverviewSnapshot(date=day, things=things, activity=activity)

    def _things(self, day: str) -> OverviewThings:
        if self.things_view is None:
            return OverviewThings(available=False)
        value: ThingsOverview = self.things_view.overview(day=day)
        return OverviewThings(
            available=True,
            open=value.open,
            scheduled_today=value.scheduled_today,
            overdue=value.overdue,
            deadline_passed=value.deadline_passed,
            completed_today=value.completed_today,
        )

    def _activity(self, day: str, week_start: str) -> OverviewActivity:
        if self.timeline_view is None:
            return OverviewActivity(available=False)
        days = self.timeline_view.overview(limit=14)
        today = next((item for item in days if item.day == day), None)
        return OverviewActivity(
            available=True,
            today_seconds=float(today.duration_seconds if today is not None else 0),
            week_seconds=sum(
                float(item.duration_seconds)
                for item in days
                if week_start <= item.day <= day
            ),
            today_apps=[
                OverviewAppActivity(
                    app_name=item.app_name,
                    bundle_id=item.bundle_id,
                    duration_seconds=float(item.duration_seconds),
                )
                for item in (today.apps if today is not None else [])
            ],
        )
