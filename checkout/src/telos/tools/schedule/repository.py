"""Persistent Kimi-style schedule task store."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from telos.core.clock import ensure_user_time, parse_user_time, user_now_iso, user_time_iso
from telos.core.ids import generate_id
from telos.storage.filesystem import read_json, write_json_atomic
from telos.tools.schedule.expression import parse_schedule_expression


DEFAULT_STALE_AFTER_DAYS = 7
DEFAULT_JITTER_SECONDS = 0
MAX_PROMPT_CHARS = 8 * 1024
MAX_ACTIVE_TASKS = 50
CLAIM_TIMEOUT_MINUTES = 10
EXECUTION_MODES = {"directMessage", "agentTurn"}


class ScheduleError(ValueError):
    """Expected schedule task failure."""


@dataclass(frozen=True)
class ScheduleTask:
    id: str
    cron: str
    prompt: str
    recurring: bool = True
    execution_mode: str = "agentTurn"
    status: str = "pending"
    created_at: str = ""
    updated_at: str = ""
    next_scheduled_at: str = ""
    next_fire_at: str = ""
    last_scheduled_at: str = ""
    last_fire_at: str = ""
    claimed_fire_key: str = ""
    claimed_at: str = ""
    completed_runs: int = 0
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS
    jitter_seconds: int = DEFAULT_JITTER_SECONDS
    last_error: str = ""

    @classmethod
    def create(
        cls,
        *,
        cron: str,
        prompt: str,
        recurring: bool,
        execution_mode: str,
        now: datetime | None = None,
        stale_after_days: int = DEFAULT_STALE_AFTER_DAYS,
        jitter_seconds: int = DEFAULT_JITTER_SECONDS,
    ) -> "ScheduleTask":
        clean_cron = _validate_cron(cron)
        clean_prompt = _validate_prompt(prompt)
        clean_mode = _validate_execution_mode(execution_mode)
        clean_stale_after_days = _normalize_non_negative_int(
            stale_after_days,
            default=DEFAULT_STALE_AFTER_DAYS,
            field_name="staleAfterDays",
        )
        clean_jitter_seconds = _normalize_non_negative_int(
            jitter_seconds,
            default=DEFAULT_JITTER_SECONDS,
            field_name="jitterSeconds",
        )
        created_at = user_time_iso(now, timespec="seconds")
        task_id = generate_id("sch")
        next_scheduled = next_schedule_due_at(
            clean_cron,
            after=now,
        )
        return cls(
            id=task_id,
            cron=clean_cron,
            prompt=clean_prompt,
            recurring=bool(recurring),
            execution_mode=clean_mode,
            created_at=created_at,
            updated_at=created_at,
            next_scheduled_at=next_scheduled.isoformat(timespec="seconds"),
            next_fire_at=_fire_at_iso(
                task_id=task_id,
                scheduled_at=next_scheduled,
                jitter_seconds=clean_jitter_seconds,
            ),
            stale_after_days=clean_stale_after_days,
            jitter_seconds=clean_jitter_seconds,
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ScheduleTask":
        return cls(
            id=str(raw["id"]),
            cron=str(raw["cron"]),
            prompt=str(raw.get("prompt") or ""),
            recurring=bool(raw.get("recurring", True)),
            execution_mode=_validate_execution_mode(raw.get("execution_mode") or "agentTurn"),
            status=str(raw.get("status") or "pending"),
            created_at=str(raw.get("created_at") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            next_scheduled_at=str(raw.get("next_scheduled_at") or ""),
            next_fire_at=str(raw.get("next_fire_at") or ""),
            last_scheduled_at=str(raw.get("last_scheduled_at") or ""),
            last_fire_at=str(raw.get("last_fire_at") or ""),
            claimed_fire_key=str(raw.get("claimed_fire_key") or ""),
            claimed_at=str(raw.get("claimed_at") or ""),
            completed_runs=_int_or_default(raw.get("completed_runs"), 0),
            stale_after_days=_normalize_non_negative_int(
                raw.get("stale_after_days"),
                default=DEFAULT_STALE_AFTER_DAYS,
                field_name="staleAfterDays",
            ),
            jitter_seconds=_normalize_non_negative_int(
                raw.get("jitter_seconds"),
                default=DEFAULT_JITTER_SECONDS,
                field_name="jitterSeconds",
            ),
            last_error=str(raw.get("last_error") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "cron": self.cron,
            "prompt": self.prompt,
            "recurring": self.recurring,
            "execution_mode": self.execution_mode,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "next_scheduled_at": self.next_scheduled_at,
            "next_fire_at": self.next_fire_at,
            "last_scheduled_at": self.last_scheduled_at,
            "last_fire_at": self.last_fire_at,
            "claimed_fire_key": self.claimed_fire_key,
            "claimed_at": self.claimed_at,
            "completed_runs": self.completed_runs,
            "stale_after_days": self.stale_after_days,
            "jitter_seconds": self.jitter_seconds,
            "last_error": self.last_error,
        }

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "cron": self.cron,
            "prompt": self.prompt,
            "recurring": self.recurring,
            "executionMode": self.execution_mode,
            "status": self.status,
            "nextFireAt": self.next_fire_at,
            "nextScheduledAt": self.next_scheduled_at,
            "createdAt": self.created_at,
            "completedRuns": self.completed_runs,
            "stale": self.is_stale(),
            "ageDays": self.age_days(),
            "lastError": self.last_error,
        }

    def with_claim(self, *, fire_key: str, claimed_at: str) -> "ScheduleTask":
        return replace(
            self,
            status="running",
            claimed_fire_key=fire_key,
            claimed_at=claimed_at,
            updated_at=claimed_at,
            last_error="",
        )

    def mark_failed(self, error: str) -> "ScheduleTask":
        now = user_now_iso()
        return replace(
            self,
            status="failed",
            claimed_fire_key="",
            claimed_at="",
            updated_at=now,
            last_error=str(error or "unknown error")[:1000],
        )

    def is_stale(self, *, now: datetime | None = None) -> bool:
        if not self.recurring or self.stale_after_days <= 0:
            return False
        created = parse_schedule_timestamp(self.created_at)
        current = ensure_user_time(now)
        return current >= created + timedelta(days=self.stale_after_days)

    def age_days(self, *, now: datetime | None = None) -> int:
        created = parse_schedule_timestamp(self.created_at)
        current = ensure_user_time(now)
        delta = current - created
        return max(0, delta.days)


@dataclass(frozen=True)
class ScheduleFire:
    task: ScheduleTask
    fire_key: str
    scheduled_at: str
    fire_at: str
    coalesced_count: int = 1
    stale: bool = False


class ScheduleRepository:
    def __init__(self, path: Path):
        self.path = path

    def list(self) -> list[ScheduleTask]:
        data = read_json(self.path, {"tasks": []})
        return [
            ScheduleTask.from_dict(item)
            for item in list(data.get("tasks") or [])
            if isinstance(item, dict)
        ]

    def save_all(self, tasks: list[ScheduleTask]) -> None:
        write_json_atomic(self.path, {"version": 1, "tasks": [task.to_dict() for task in tasks]})

    def add(self, task: ScheduleTask) -> ScheduleTask:
        tasks = self.list()
        tasks.append(task)
        self.save_all(tasks)
        return task

    def delete(self, task_id: str) -> bool:
        tasks = self.list()
        remaining = [task for task in tasks if task.id != task_id]
        if len(remaining) == len(tasks):
            return False
        self.save_all(remaining)
        return True


def next_schedule_due_at(
    cron: str,
    *,
    after: datetime | None = None,
    max_lookahead_days: int = 366,
) -> datetime:
    schedule = parse_schedule_expression(cron)
    after_dt = ensure_user_time(after)
    cursor = after_dt.replace(second=0, microsecond=0)
    if cursor <= after_dt:
        cursor += timedelta(minutes=1)
    end = cursor + timedelta(days=max_lookahead_days)
    while cursor <= end:
        if schedule.matches(cursor):
            return cursor
        cursor += timedelta(minutes=1)
    raise ScheduleError("could not find next schedule fire time within lookahead window")


def parse_schedule_timestamp(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        return ensure_user_time()
    try:
        return parse_user_time(text)
    except ValueError as error:
        raise ScheduleError(f"invalid schedule timestamp: {value}") from error


def _validate_cron(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ScheduleError("cron is required")
    try:
        parse_schedule_expression(text)
    except ValueError as error:
        raise ScheduleError(f"invalid cron: {error}") from error
    return text


def _validate_prompt(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ScheduleError("prompt is required")
    if len(text) > MAX_PROMPT_CHARS:
        raise ScheduleError(f"prompt must be at most {MAX_PROMPT_CHARS} characters")
    return text


def _validate_execution_mode(value: Any) -> str:
    text = str(value or "agentTurn").strip() or "agentTurn"
    aliases = {
        "message": "directMessage",
        "direct_message": "directMessage",
        "direct-message": "directMessage",
        "agent": "agentTurn",
        "agent_turn": "agentTurn",
        "agent-turn": "agentTurn",
    }
    text = aliases.get(text, text)
    if text not in EXECUTION_MODES:
        raise ScheduleError("executionMode must be directMessage or agentTurn")
    return text


def _fire_at_iso(*, task_id: str, scheduled_at: datetime, jitter_seconds: int) -> str:
    seconds = 0
    if jitter_seconds > 0:
        import hashlib

        key = f"{task_id}:{scheduled_at.isoformat(timespec='seconds')}"
        seconds = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16) % (jitter_seconds + 1)
    return (scheduled_at + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _normalize_non_negative_int(value: Any, *, default: int, field_name: str) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ScheduleError(f"{field_name} must be an integer") from error
    if parsed < 0:
        raise ScheduleError(f"{field_name} must be non-negative")
    return parsed


def _int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
