"""Schedule task service."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta

from telos.core.clock import ensure_user_time
from telos.tools.schedule.repository import (
    CLAIM_TIMEOUT_MINUTES,
    MAX_ACTIVE_TASKS,
    DEFAULT_JITTER_SECONDS,
    DEFAULT_STALE_AFTER_DAYS,
    ScheduleError,
    ScheduleFire,
    ScheduleRepository,
    ScheduleTask,
    next_schedule_due_at,
    parse_schedule_timestamp,
)


class ScheduleService:
    def __init__(self, repository: ScheduleRepository):
        self.repository = repository

    def create(
        self,
        *,
        cron: str,
        prompt: str,
        recurring: bool = True,
        execution_mode: str = "agentTurn",
        now: datetime | None = None,
        stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
        jitter_seconds: int = DEFAULT_JITTER_SECONDS,
    ) -> ScheduleTask:
        _ensure_enabled()
        task = ScheduleTask.create(
            cron=cron,
            prompt=prompt,
            recurring=recurring,
            execution_mode=execution_mode,
            now=now,
            stale_after_days=stale_after_days,
            jitter_seconds=jitter_seconds,
        )
        active = [
            item
            for item in self.repository.list()
            if item.status not in {"completed", "cancelled"}
        ]
        if len(active) >= MAX_ACTIVE_TASKS:
            raise ScheduleError(
                f"最多只能保留 {MAX_ACTIVE_TASKS} 个 active schedule"
            )
        return self.repository.add(task)

    def list(
        self,
        *,
        include_failed: bool = True,
    ) -> list[ScheduleTask]:
        tasks = self.repository.list()
        visible = [task for task in tasks if task.status not in {"completed", "cancelled"}]
        if not include_failed:
            visible = [task for task in visible if task.status != "failed"]
        visible.sort(key=lambda task: (task.next_fire_at, task.created_at, task.id))
        return visible

    def delete(self, task_id: str) -> bool:
        _ensure_enabled()
        clean_id = str(task_id or "").strip()
        if not clean_id:
            raise ScheduleError("taskId is required")
        return self.repository.delete(clean_id)

    def claim_due(
        self,
        *,
        now: datetime | None = None,
        limit: int = 10,
    ) -> list[ScheduleFire]:
        _ensure_enabled()
        now_dt = ensure_user_time(now)
        now_iso = now_dt.isoformat(timespec="seconds")
        tasks = self.repository.list()
        due_indices: list[int] = []
        for index, task in enumerate(tasks):
            if task.status == "running":
                if _claim_is_active(task, now=now_dt):
                    continue
            elif task.status != "pending":
                continue
            fire_at = parse_schedule_timestamp(task.next_fire_at)
            if fire_at <= now_dt:
                due_indices.append(index)
        due_indices.sort(key=lambda index: (tasks[index].next_fire_at, tasks[index].created_at, tasks[index].id))

        fires: list[ScheduleFire] = []
        for index in due_indices[: max(0, int(limit))]:
            task = tasks[index]
            coalesced_count = _coalesced_count(task, now=now_dt)
            stale = task.is_stale(now=now_dt)
            fire_key = str(task.next_fire_at or task.next_scheduled_at or task.id)
            claimed = task.with_claim(fire_key=fire_key, claimed_at=now_iso)
            tasks[index] = claimed
            fires.append(
                ScheduleFire(
                    task=claimed,
                    fire_key=fire_key,
                    scheduled_at=task.next_scheduled_at,
                    fire_at=task.next_fire_at,
                    coalesced_count=coalesced_count,
                    stale=stale,
                )
            )
        if fires:
            self.repository.save_all(tasks)
        return fires

    def active_claim_keys(self) -> set[tuple[str, str]]:
        return {
            (task.id, str(task.claimed_fire_key or task.next_fire_at or task.next_scheduled_at))
            for task in self.repository.list()
            if task.status == "running"
        }

    def release_active_claims(self, *, now: datetime | None = None) -> int:
        timestamp = ensure_user_time(now).isoformat(timespec="seconds")
        tasks = self.repository.list()
        released = 0
        for index, task in enumerate(tasks):
            if task.status != "running":
                continue
            tasks[index] = replace(
                task,
                status="pending",
                claimed_fire_key="",
                claimed_at="",
                updated_at=timestamp,
            )
            released += 1
        if released:
            self.repository.save_all(tasks)
        return released

    def release_claims(self, fires: list[ScheduleFire], *, now: datetime | None = None) -> int:
        keys = {(fire.task.id, fire.fire_key) for fire in fires}
        if not keys:
            return 0
        timestamp = ensure_user_time(now).isoformat(timespec="seconds")
        tasks = self.repository.list()
        released = 0
        for index, task in enumerate(tasks):
            key = (task.id, str(task.claimed_fire_key or task.next_fire_at or task.next_scheduled_at))
            if task.status != "running" or key not in keys:
                continue
            tasks[index] = replace(
                task,
                status="pending",
                claimed_fire_key="",
                claimed_at="",
                updated_at=timestamp,
            )
            released += 1
        if released:
            self.repository.save_all(tasks)
        return released

    def mark_completed(
        self,
        task_id: str,
        *,
        fire_key: str = "",
        now: datetime | None = None,
    ) -> bool:
        now_dt = ensure_user_time(now)
        tasks = self.repository.list()
        for index, task in enumerate(tasks):
            if task.id != task_id:
                continue
            if fire_key and task.claimed_fire_key and task.claimed_fire_key != fire_key:
                return False
            if not task.recurring or task.is_stale(now=now_dt):
                del tasks[index]
                self.repository.save_all(tasks)
                return True
            next_scheduled = next_schedule_due_at(
                task.cron,
                after=now_dt,
            )
            now_iso = now_dt.isoformat(timespec="seconds")
            updated = replace(
                task,
                status="pending",
                completed_runs=task.completed_runs + 1,
                last_scheduled_at=task.next_scheduled_at,
                last_fire_at=now_iso,
                next_scheduled_at=next_scheduled.isoformat(timespec="seconds"),
                next_fire_at=_next_fire_at(task, next_scheduled),
                claimed_fire_key="",
                claimed_at="",
                updated_at=now_iso,
                last_error="",
            )
            tasks[index] = updated
            self.repository.save_all(tasks)
            return True
        return False

    def mark_failed(self, task_id: str, error: str, *, fire_key: str = "") -> bool:
        tasks = self.repository.list()
        for index, task in enumerate(tasks):
            if task.id != task_id:
                continue
            if fire_key and task.claimed_fire_key and task.claimed_fire_key != fire_key:
                return False
            tasks[index] = task.mark_failed(error)
            self.repository.save_all(tasks)
            return True
        return False


def _ensure_enabled() -> None:
    if str(os.environ.get("TELOS_DISABLE_SCHEDULE") or "").strip() in {"1", "true", "yes"}:
        raise ScheduleError("schedule tools are disabled by TELOS_DISABLE_SCHEDULE")


def _claim_is_active(task: ScheduleTask, *, now: datetime) -> bool:
    if not task.claimed_fire_key:
        return False
    if not str(task.claimed_at or "").strip():
        return False
    claimed_at = parse_schedule_timestamp(task.claimed_at)
    return now - claimed_at <= timedelta(minutes=CLAIM_TIMEOUT_MINUTES)


def _coalesced_count(task: ScheduleTask, *, now: datetime) -> int:
    if not task.recurring:
        return 1
    count = 1
    cursor = parse_schedule_timestamp(task.next_scheduled_at)
    while count < 10000:
        next_scheduled = next_schedule_due_at(
            task.cron,
            after=cursor,
        )
        if next_scheduled > now:
            break
        count += 1
        cursor = next_scheduled
    return count


def _next_fire_at(task: ScheduleTask, next_scheduled: datetime) -> str:
    # Keep jitter behavior deterministic for an existing task id.
    from telos.tools.schedule.repository import _fire_at_iso

    return _fire_at_iso(
        task_id=task.id,
        scheduled_at=next_scheduled,
        jitter_seconds=task.jitter_seconds,
    )
