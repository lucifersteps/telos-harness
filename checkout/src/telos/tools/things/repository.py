"""SQLite-backed Things repository."""

from __future__ import annotations

import calendar
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from telos.core.clock import ensure_user_time, user_now, user_now_iso
from telos.core.ids import generate_id
from telos.storage.filesystem import ensure_dir


THING_TYPES = {"domain", "list", "section", "task", "event"}
CONTAINER_TYPES = {"domain", "list", "section"}
ITEM_TYPES = {"task", "event"}
STATUSES = {"open", "completed", "archived"}
SCHEDULES = {"someday", "suspended", "anytime", "scheduled"}
WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
THING_REMINDER_CLAIM_TIMEOUT_MINUTES = 10


@dataclass(frozen=True)
class ThingRef:
    id: str
    type: str
    title: str
    status: str
    domain_id: str = ""
    list_id: str = ""
    section_id: str = ""


@dataclass(frozen=True)
class ThingReminderFire:
    id: str
    reminder_id: str
    thing_id: str
    fire_key: str
    fire_at: str
    thing_type: str
    title: str
    reminder_kind: str
    occurrence_date: str = ""
    when: str = ""
    deadline: str = ""
    location: str = ""
    domain_title: str = ""
    list_title: str = ""
    section_title: str = ""


class ThingsError(ValueError):
    """Expected Things data failure."""


class ThingsRepository:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()
        ensure_dir(self.path.parent)
        self._connection = sqlite3.connect(str(self.path))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    @classmethod
    def for_agent_dir(cls, agent_dir: Path) -> "ThingsRepository":
        return cls(Path(agent_dir) / "runtime" / "things.sqlite")

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "ThingsRepository":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.close()

    def create(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        created: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        with self._connection:
            for raw in _dict_items(items):
                # 单项失败必须整体回滚该项：否则中途异常会留下没有扩展行的孤儿记录，
                # 后续 browse/detail 直接 500。
                self._connection.execute("SAVEPOINT create_one")
                try:
                    item_id = self._create_one(raw)
                    self._connection.execute("RELEASE create_one")
                    created.append(self._summary(item_id))
                except Exception as error:  # noqa: BLE001 - normalize tool boundary failures.
                    self._connection.execute("ROLLBACK TO create_one")
                    self._connection.execute("RELEASE create_one")
                    failed.append({"item": raw, "error": str(error)})
        return {"created": created, "failed": failed}

    def update(self, updates: list[dict[str, Any]]) -> dict[str, Any]:
        updated: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        with self._connection:
            for raw in _dict_items(updates):
                self._connection.execute("SAVEPOINT update_one")
                try:
                    item_id, notes = self._update_one(raw)
                    self._connection.execute("RELEASE update_one")
                    summary = self._summary(item_id)
                    if notes:
                        summary["notes"] = notes
                    updated.append(summary)
                except Exception as error:  # noqa: BLE001 - normalize tool boundary failures.
                    self._connection.execute("ROLLBACK TO update_one")
                    self._connection.execute("RELEASE update_one")
                    failed.append({"item": raw, "error": str(error)})
        return {"updated": updated, "failed": failed}

    def delete(self, requests: list[dict[str, Any]]) -> dict[str, Any]:
        """软删除对象；deleted 只能由专用入口写入，不能由 update 写入。"""
        deleted: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        with self._connection:
            for raw in _dict_items(requests):
                try:
                    target = raw.get("target") if isinstance(raw.get("target"), dict) else raw
                    item_id = self._resolve_target(target)
                    before = self._detail(item_id)
                    previous_status = str(before.get("status") or "open")
                    if previous_status == "deleted":
                        raise ThingsError("对象已经删除")
                    now = user_now_iso()
                    self._connection.execute(
                        "UPDATE things SET status = 'deleted', completed_at = '', updated_at = ? WHERE id = ?",
                        (now, item_id),
                    )
                    after = self._detail(item_id)
                    self._audit("delete", item_id, before, after)
                    summary = self._summary(item_id)
                    summary["previous_status"] = previous_status
                    deleted.append(summary)
                except Exception as error:  # noqa: BLE001 - normalize web boundary failures.
                    failed.append({"item": raw, "error": str(error)})
        return {"deleted": deleted, "failed": failed}

    def restore_deleted(self, requests: list[dict[str, Any]]) -> dict[str, Any]:
        """恢复软删除对象；目标状态仍只允许现有可更新状态。"""
        restored: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        with self._connection:
            for raw in _dict_items(requests):
                try:
                    target = raw.get("target") if isinstance(raw.get("target"), dict) else raw
                    item_id = self._resolve_target(target)
                    before = self._detail(item_id)
                    if str(before.get("status") or "") != "deleted":
                        raise ThingsError("对象没有被删除")
                    item_type = str(before.get("type") or "")
                    status = _clean_status(raw.get("status"), item_type=item_type)
                    now = user_now_iso()
                    self._connection.execute(
                        "UPDATE things SET status = ?, completed_at = ?, updated_at = ? WHERE id = ?",
                        (status, now if status == "completed" else "", now, item_id),
                    )
                    after = self._detail(item_id)
                    self._audit("restore", item_id, before, after)
                    restored.append(self._summary(item_id))
                except Exception as error:  # noqa: BLE001 - normalize web boundary failures.
                    failed.append({"item": raw, "error": str(error)})
        return {"restored": restored, "failed": failed}

    def detail(self, requests: list[dict[str, Any]]) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for raw in _dict_items(requests):
            try:
                item_id = self._resolve_detail_request(raw)
                items.append(self._detail(item_id))
            except Exception as error:  # noqa: BLE001 - normalize tool boundary failures.
                failed.append({"item": raw, "error": str(error)})
        return {"items": items, "failed": failed}

    def list(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        data = dict(filters or {})
        types = _type_filter(data.get("type"), default=["task", "event"])
        self._apply_default_list_filters(data, types)
        if any(item in CONTAINER_TYPES for item in types):
            items = self._list_containers(data, types)
            total = len(items)
            limit = _positive_int(data.get("limit"), default=100)
            return {"items": [_public_container(item) for item in items[:limit]], "total": total, "cursor": None}
        else:
            items = self._list_items(data, types)
        total = len(items)
        limit = _positive_int(data.get("limit"), default=100)
        return {"items": self._tree_items(items[:limit]), "total": total, "cursor": None}

    def import_external_ref(
        self,
        *,
        thing_id: str,
        provider: str,
        provider_type: str,
        external_id: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO thing_external_refs (
                thing_id, provider, provider_type, external_id, deleted_at
            ) VALUES (?, ?, ?, ?, '')
            ON CONFLICT(provider, external_id) DO UPDATE SET
                thing_id = excluded.thing_id,
                provider_type = excluded.provider_type,
                deleted_at = ''
            """,
            (
                str(thing_id),
                str(provider),
                str(provider_type),
                str(external_id),
            ),
        )

    def external_ref(self, provider: str, external_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            """
            SELECT * FROM thing_external_refs
            WHERE provider = ? AND external_id = ? AND deleted_at = ''
            """,
            (str(provider), str(external_id)),
        ).fetchone()
        return dict(row) if row else None

    def external_refs_for(self, thing_id: str, *, provider: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM thing_external_refs WHERE thing_id = ? AND deleted_at = ''"
        values: list[Any] = [str(thing_id)]
        if provider:
            sql += " AND provider = ?"
            values.append(str(provider))
        rows = self._connection.execute(sql, values).fetchall()
        return [dict(row) for row in rows]

    def claim_due_reminders(
        self,
        *,
        now: datetime | None = None,
        limit: int = 10,
    ) -> list[ThingReminderFire]:
        now_dt = ensure_user_time(now)
        claimed_at = _format_datetime(now_dt)
        candidates = self._due_reminder_candidates(now_dt)
        claimed: list[ThingReminderFire] = []
        with self._connection:
            for candidate in candidates:
                if len(claimed) >= max(0, int(limit or 0)):
                    break
                existing = self._reminder_fire_row(
                    candidate.reminder_id,
                    candidate.fire_key,
                )
                if existing is not None:
                    status = str(existing["status"] or "")
                    if status == "completed":
                        continue
                    if status == "running" and _reminder_claim_is_active(
                        str(existing["claimed_at"] or ""),
                        now_dt,
                    ):
                        continue
                    fire_id = str(existing["id"])
                    self._connection.execute(
                        """
                        UPDATE thing_reminder_fires
                        SET status = 'running', claimed_at = ?, last_error = ''
                        WHERE id = ?
                        """,
                        (claimed_at, fire_id),
                    )
                else:
                    fire_id = generate_id("thgremfire")
                    self._connection.execute(
                        """
                        INSERT INTO thing_reminder_fires (
                            id, reminder_id, thing_id, fire_key, fire_at,
                            status, claimed_at, completed_at, last_error
                        ) VALUES (?, ?, ?, ?, ?, 'running', ?, '', '')
                        """,
                        (
                            fire_id,
                            candidate.reminder_id,
                            candidate.thing_id,
                            candidate.fire_key,
                            candidate.fire_at,
                            claimed_at,
                        ),
                    )
                claimed.append(
                    ThingReminderFire(
                        id=fire_id,
                        reminder_id=candidate.reminder_id,
                        thing_id=candidate.thing_id,
                        fire_key=candidate.fire_key,
                        fire_at=candidate.fire_at,
                        thing_type=candidate.thing_type,
                        title=candidate.title,
                        reminder_kind=candidate.reminder_kind,
                        occurrence_date=candidate.occurrence_date,
                        when=candidate.when,
                        deadline=candidate.deadline,
                        location=candidate.location,
                        domain_title=candidate.domain_title,
                        list_title=candidate.list_title,
                        section_title=candidate.section_title,
                    )
                )
        return claimed

    def mark_reminder_fire_completed(
        self,
        fire_id: str,
        *,
        completed_at: datetime | None = None,
    ) -> None:
        timestamp = _format_datetime(ensure_user_time(completed_at))
        with self._connection:
            self._connection.execute(
                """
                UPDATE thing_reminder_fires
                SET status = 'completed', completed_at = ?, claimed_at = '', last_error = ''
                WHERE id = ?
                """,
                (timestamp, str(fire_id)),
            )

    def mark_reminder_fire_failed(self, fire_id: str, error: str) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE thing_reminder_fires
                SET status = 'failed', claimed_at = '', last_error = ?
                WHERE id = ?
                """,
                (str(error or "unknown error")[:1000], str(fire_id)),
            )

    def active_reminder_fire_keys(self) -> set[tuple[str, str]]:
        rows = self._connection.execute(
            """
            SELECT reminder_id, fire_key FROM thing_reminder_fires
            WHERE status = 'running'
            """
        ).fetchall()
        return {
            (str(row["reminder_id"] or ""), str(row["fire_key"] or ""))
            for row in rows
        }

    def release_running_reminder_fires(self, *, error: str = "daemon startup recovery") -> int:
        with self._connection:
            cursor = self._connection.execute(
                """
                UPDATE thing_reminder_fires
                SET status = 'failed', claimed_at = '', last_error = ?
                WHERE status = 'running'
                """,
                (str(error or "daemon startup recovery")[:1000],),
            )
        return int(cursor.rowcount or 0)

    def release_reminder_fires(
        self,
        fires: list[ThingReminderFire],
        *,
        error: str = "released for retry",
    ) -> int:
        fire_ids = [str(fire.id or "").strip() for fire in fires if str(fire.id or "").strip()]
        if not fire_ids:
            return 0
        with self._connection:
            cursor = self._connection.executemany(
                """
                UPDATE thing_reminder_fires
                SET status = 'failed', claimed_at = '', last_error = ?
                WHERE id = ?
                """,
                [(str(error or "released for retry")[:1000], fire_id) for fire_id in fire_ids],
            )
        return int(cursor.rowcount or 0)

    def _create_one(self, raw: dict[str, Any]) -> str:
        item_type = _clean_type(raw.get("type"))
        now = user_now_iso()
        item_id = _new_thing_id(item_type)
        title = _required_text(raw.get("title"), "title")
        description = _optional_text(raw.get("description"))
        domain_id, list_id, section_id = self._container_ids(raw, item_type)
        status = _clean_status(raw.get("status") or "open", item_type=item_type)
        position = self._next_position(item_type, domain_id, list_id, section_id)

        self._connection.execute(
            """
            INSERT INTO things (
                id, type, title, description, status, domain_id, list_id, section_id,
                position, created_at, updated_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                item_type,
                title,
                description,
                status,
                domain_id,
                list_id,
                section_id,
                position,
                now,
                now,
                now if status == "completed" else "",
            ),
        )
        if item_type == "task":
            self._create_task(item_id, raw)
        elif item_type == "event":
            self._create_event(item_id, raw)
        self._replace_tags(item_id, raw.get("tags"))
        self._replace_attachments(item_id, raw.get("attachments"))
        if item_type in ITEM_TYPES:
            self._replace_reminders(item_id, raw.get("reminder"))
            self._replace_recurrence(item_id, raw.get("recurrence"))
        if item_type == "task":
            self._replace_blocked_by(item_id, raw.get("blocked_by"))
        self._audit("create", item_id, {}, self._detail(item_id))
        return item_id

    def _next_position(
        self,
        item_type: str,
        domain_id: str,
        list_id: str,
        section_id: str,
    ) -> int:
        values: list[Any]
        if item_type in ITEM_TYPES:
            sql = """
                SELECT COALESCE(MAX(position), -1) AS position
                FROM things
                WHERE type IN ('task', 'event')
                  AND status != 'deleted'
                  AND domain_id = ? AND list_id = ? AND section_id = ?
            """
            values = [domain_id, list_id, section_id]
        else:
            values = [item_type]
            sql = "SELECT COALESCE(MAX(position), -1) AS position FROM things WHERE type = ? AND status != 'deleted'"
        if item_type == "list":
            sql += " AND domain_id = ?"
            values.append(domain_id)
        elif item_type == "section":
            sql += " AND list_id = ?"
            values.append(list_id)
        row = self._connection.execute(sql, values).fetchone()
        return int(row["position"] if row["position"] is not None else -1) + 1

    def _create_task(self, item_id: str, raw: dict[str, Any]) -> None:
        defer_until = _normalize_defer(raw.get("defer_until"))
        if _is_deferred(defer_until):
            _reject_deferred_time_fields(raw, ("scheduled_at", "reminder", "recurrence"))
        scheduled_at, precision = _normalize_item_time(raw.get("scheduled_at"), keep_date=True)
        deadline = _normalize_deadline(raw.get("deadline"))
        parent_id = self._resolve_optional_task_ref(raw.get("parent_task"))
        location = _optional_text(raw.get("location"))
        self._connection.execute(
            """
            INSERT INTO tasks (
                thing_id, parent_task_id, defer_until, deadline_at,
                scheduled_at, scheduled_precision, fixed, location
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                parent_id,
                defer_until,
                deadline,
                scheduled_at,
                precision,
                1 if bool(raw.get("fixed")) else 0,
                location,
            ),
        )
        if parent_id:
            self._ensure_no_parent_cycle(item_id)

    def _create_event(self, item_id: str, raw: dict[str, Any]) -> None:
        defer_until = _normalize_defer(raw.get("defer_until"))
        if _is_deferred(defer_until):
            _reject_deferred_time_fields(raw, ("start_at", "duration", "reminder", "recurrence"))
        start_at, precision = _normalize_item_time(raw.get("start_at"), keep_date=False)
        deadline = _normalize_deadline(raw.get("deadline"))
        duration_minutes = _duration_minutes(raw.get("duration"))
        location = _optional_text(raw.get("location"))
        self._connection.execute(
            """
            INSERT INTO events (
                thing_id, defer_until, start_at, start_precision,
                duration_minutes, fixed, deadline_at, location
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                defer_until,
                start_at,
                precision,
                duration_minutes,
                1 if bool(raw.get("fixed")) else 0,
                deadline,
                location,
            ),
        )

    def _update_one(self, raw: dict[str, Any]) -> tuple[str, list[str]]:
        target = raw.get("target")
        if not isinstance(target, dict):
            if "id" in raw:
                target = {"id": raw.get("id")}
            else:
                raise ThingsError("target is required")
        item_id = self._resolve_target(target)
        before = self._detail(item_id)
        row = self._thing_row(item_id)
        if str(row["status"] or "") == "deleted":
            raise ThingsError("已删除对象只能通过恢复入口处理")
        item_type = str(row["type"])
        occurrence_date = _optional_occurrence_date(target.get("occurrence_date"))
        if occurrence_date:
            return self._update_occurrence(item_id, item_type, occurrence_date, raw, before)
        if (
            item_type == "task"
            and raw.get("status") == "completed"
            and self._has_recurrence(item_id)
        ):
            occurrence = self._current_occurrence_date(item_id)
            if occurrence:
                return self._update_occurrence(item_id, item_type, occurrence, raw, before)
        new_type = str(raw.get("type") or item_type)
        if new_type != item_type:
            _clean_status(raw.get("status") or row["status"], item_type=new_type)
            item_type = self._convert_type(item_id, item_type, new_type)
        now = user_now_iso()
        updates: dict[str, Any] = {}
        notes: list[str] = []
        if "title" in raw:
            updates["title"] = _required_text(raw.get("title"), "title")
        if "description" in raw:
            updates["description"] = _optional_text(raw.get("description"))
        if any(key in raw for key in ("domain", "list", "section")):
            container_update = {
                "domain": raw.get("domain", {"id": row["domain_id"]} if row["domain_id"] else None),
                "list": raw.get("list", {"id": row["list_id"]} if row["list_id"] else None),
                "section": raw.get("section", {"id": row["section_id"]} if row["section_id"] else None),
            }
            if "list" in raw and "section" not in raw:
                container_update["section"] = None
            domain_id, list_id, section_id = self._container_ids(
                container_update,
                item_type,
            )
            updates.update(domain_id=domain_id, list_id=list_id, section_id=section_id)
        if "position" in raw:
            try:
                updates["position"] = max(0, int(raw.get("position") or 0))
            except (TypeError, ValueError) as error:
                raise ThingsError("position 必须是非负整数") from error
        if "status" in raw:
            status = _clean_status(raw.get("status"), item_type=item_type)
            updates["status"] = status
            updates["completed_at"] = now if status == "completed" else ""
        if updates:
            assignments = ", ".join(f"{name} = ?" for name in updates)
            values = list(updates.values())
            values.extend([now, item_id])
            self._connection.execute(
                f"UPDATE things SET {assignments}, updated_at = ? WHERE id = ?",
                values,
            )
        if item_type == "task":
            notes.extend(self._update_task_fields(item_id, raw))
        elif item_type == "event":
            notes.extend(self._update_event_fields(item_id, raw))
        if "tags" in raw:
            self._replace_tags(item_id, raw.get("tags"))
        if "attachments" in raw:
            self._replace_attachments(item_id, raw.get("attachments"))
        if "reminder" in raw and item_type in ITEM_TYPES:
            self._replace_reminders(item_id, raw.get("reminder"))
        if "recurrence" in raw and item_type in ITEM_TYPES:
            self._replace_recurrence(item_id, raw.get("recurrence"))
        if "blocked_by" in raw and item_type == "task":
            self._replace_blocked_by(item_id, raw.get("blocked_by"))
        self._touch(item_id)
        after = self._detail(item_id)
        self._audit("update", item_id, before, after)
        return item_id, notes

    def _update_task_fields(self, item_id: str, raw: dict[str, Any]) -> list[str]:
        current = self._task_row(item_id)
        values = {
            "parent_task_id": current["parent_task_id"],
            "defer_until": current["defer_until"],
            "deadline_at": current["deadline_at"],
            "scheduled_at": current["scheduled_at"],
            "scheduled_precision": current["scheduled_precision"],
            "fixed": current["fixed"],
            "location": current["location"],
        }
        notes: list[str] = []
        if "parent_task" in raw:
            values["parent_task_id"] = self._resolve_optional_task_ref(raw.get("parent_task"))
        if "defer_until" in raw:
            values["defer_until"] = _normalize_defer(raw.get("defer_until"))
            if _is_deferred(values["defer_until"]):
                if values["scheduled_at"]:
                    notes.append("已清空 scheduled_at")
                values["scheduled_at"] = ""
                values["scheduled_precision"] = ""
                self._replace_reminders(item_id, [])
                self._replace_recurrence(item_id, None)
                notes.extend(["已清空 reminder", "已清空 recurrence"])
        if "deadline" in raw:
            values["deadline_at"] = _normalize_deadline(raw.get("deadline"))
        if "scheduled_at" in raw:
            if _has_field_value(raw.get("scheduled_at")):
                _ensure_not_deferred(values["defer_until"], "scheduled_at")
            scheduled_at, precision = _normalize_item_time(raw.get("scheduled_at"), keep_date=True)
            values["scheduled_at"] = scheduled_at
            values["scheduled_precision"] = precision
        for field in ("reminder", "recurrence"):
            if _has_field_value(raw.get(field)):
                _ensure_not_deferred(values["defer_until"], field)
        if "fixed" in raw:
            values["fixed"] = 1 if bool(raw.get("fixed")) else 0
        if "location" in raw:
            values["location"] = _optional_text(raw.get("location"))
        self._connection.execute(
            """
            UPDATE tasks SET parent_task_id = ?, defer_until = ?, deadline_at = ?,
                scheduled_at = ?, scheduled_precision = ?, fixed = ?, location = ?
            WHERE thing_id = ?
            """,
            (
                values["parent_task_id"],
                values["defer_until"],
                values["deadline_at"],
                values["scheduled_at"],
                values["scheduled_precision"],
                values["fixed"],
                values["location"],
                item_id,
            ),
        )
        if values["parent_task_id"]:
            self._ensure_no_parent_cycle(item_id)
        return notes

    def _update_event_fields(self, item_id: str, raw: dict[str, Any]) -> list[str]:
        current = self._event_row(item_id)
        values = {
            "defer_until": current["defer_until"],
            "start_at": current["start_at"],
            "start_precision": current["start_precision"],
            "duration_minutes": current["duration_minutes"],
            "fixed": current["fixed"],
            "deadline_at": current["deadline_at"],
            "location": current["location"],
        }
        notes: list[str] = []
        if "defer_until" in raw:
            values["defer_until"] = _normalize_defer(raw.get("defer_until"))
            if _is_deferred(values["defer_until"]):
                for field in ("start_at", "duration"):
                    if values["start_at"] and field == "start_at":
                        notes.append("已清空 start_at")
                    if values["duration_minutes"] and field == "duration":
                        notes.append("已清空 duration")
                values["start_at"] = ""
                values["start_precision"] = ""
                values["duration_minutes"] = 0
                self._replace_reminders(item_id, [])
                self._replace_recurrence(item_id, None)
                notes.extend(["已清空 reminder", "已清空 recurrence"])
        if "start_at" in raw:
            if _has_field_value(raw.get("start_at")):
                _ensure_not_deferred(values["defer_until"], "start_at")
            start_at, precision = _normalize_item_time(raw.get("start_at"), keep_date=False)
            values["start_at"] = start_at
            values["start_precision"] = precision
        if "duration" in raw:
            if _has_field_value(raw.get("duration")):
                _ensure_not_deferred(values["defer_until"], "duration")
            values["duration_minutes"] = _duration_minutes(raw.get("duration"))
        for field in ("reminder", "recurrence"):
            if _has_field_value(raw.get(field)):
                _ensure_not_deferred(values["defer_until"], field)
        if "fixed" in raw:
            values["fixed"] = 1 if bool(raw.get("fixed")) else 0
        if "deadline" in raw:
            values["deadline_at"] = _normalize_deadline(raw.get("deadline"))
        if "location" in raw:
            values["location"] = _optional_text(raw.get("location"))
        self._connection.execute(
            """
            UPDATE events SET defer_until = ?, start_at = ?, start_precision = ?,
                duration_minutes = ?, fixed = ?, deadline_at = ?, location = ?
            WHERE thing_id = ?
            """,
            (
                values["defer_until"],
                values["start_at"],
                values["start_precision"],
                values["duration_minutes"],
                values["fixed"],
                values["deadline_at"],
                values["location"],
                item_id,
            ),
        )
        return notes

    def _convert_type(self, item_id: str, old_type: str, new_type: str) -> str:
        if {old_type, new_type} != {"task", "event"}:
            raise ThingsError("type 只能在 task 和 event 之间转换")
        if old_type == "task":
            if self._has_task_in_edges(item_id):
                raise ThingsError("该 task 有子任务或依赖入边，不能转成 event")
            task = self._task_row(item_id)
            self._connection.execute("DELETE FROM task_dependencies WHERE task_id = ?", (item_id,))
            self._connection.execute("DELETE FROM tasks WHERE thing_id = ?", (item_id,))
            self._connection.execute(
                """
                INSERT INTO events (
                    thing_id, defer_until, start_at, start_precision,
                    duration_minutes, fixed, deadline_at, location
                ) VALUES (?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    item_id,
                    task["defer_until"],
                    task["scheduled_at"],
                    task["scheduled_precision"],
                    task["fixed"],
                    task["deadline_at"],
                    task["location"],
                ),
            )
        else:
            event = self._event_row(item_id)
            self._connection.execute("DELETE FROM events WHERE thing_id = ?", (item_id,))
            self._connection.execute(
                """
                INSERT INTO tasks (
                    thing_id, parent_task_id, defer_until, deadline_at,
                    scheduled_at, scheduled_precision, fixed, location
                ) VALUES (?, '', ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    event["defer_until"],
                    event["deadline_at"],
                    event["start_at"],
                    event["start_precision"],
                    event["fixed"],
                    event["location"],
                ),
            )
        self._connection.execute("UPDATE things SET type = ? WHERE id = ?", (new_type, item_id))
        return new_type

    def _container_ids(self, raw: dict[str, Any], item_type: str) -> tuple[str, str, str]:
        if item_type == "domain":
            return "", "", ""
        domain_id = self._resolve_optional_ref("domain", raw.get("domain"))
        list_ref = raw.get("list")
        section_ref = raw.get("section")
        list_id = self._resolve_optional_ref("list", list_ref, domain_id=domain_id)
        section_id = self._resolve_optional_ref("section", section_ref, list_id=list_id)
        if item_type == "section":
            list_id = self._resolve_required_ref("list", list_ref, domain_id=domain_id)
            section_id = ""
        if section_id:
            section = self._thing_ref(section_id)
            list_id = _check_or_fill("list", list_id, section.list_id)
            domain_id = _check_or_fill("domain", domain_id, section.domain_id)
        if list_id:
            parent = self._thing_ref(list_id)
            domain_id = _check_or_fill("domain", domain_id, parent.domain_id)
        if item_type == "section" and not list_id:
            raise ThingsError("section 必须属于某个 list")
        return domain_id, list_id, section_id

    def _resolve_detail_request(self, raw: dict[str, Any]) -> str:
        if "id" in raw:
            return self._require_existing_id(str(raw.get("id") or ""))
        item_type = _clean_type(raw.get("type"))
        item = raw.get("item")
        if not isinstance(item, dict):
            raise ThingsError("item is required")
        if "id" in item:
            return self._require_existing_id(str(item.get("id") or ""), item_type=item_type)
        title = _required_text(item.get("title"), "title")
        return self._resolve_unique_title(item_type, title)

    def _resolve_target(self, target: dict[str, Any]) -> str:
        if "id" in target:
            return self._require_existing_id(str(target.get("id") or ""))
        item_type = _clean_type(target.get("type"))
        title = _required_text(target.get("title"), "title")
        return self._resolve_unique_title(item_type, title)

    def _resolve_optional_ref(
        self,
        item_type: str,
        ref: Any,
        *,
        domain_id: str = "",
        list_id: str = "",
    ) -> str:
        if ref is None:
            return ""
        if not isinstance(ref, dict):
            raise ThingsError(f"{item_type} must be an object or null")
        if "id" in ref:
            return self._require_existing_id(str(ref.get("id") or ""), item_type=item_type)
        if "title" in ref:
            return self._resolve_unique_title(
                item_type,
                _required_text(ref.get("title"), f"{item_type}.title"),
                domain_id=domain_id,
                list_id=list_id,
            )
        return ""

    def _resolve_required_ref(
        self,
        item_type: str,
        ref: Any,
        *,
        domain_id: str = "",
        list_id: str = "",
    ) -> str:
        resolved = self._resolve_optional_ref(
            item_type,
            ref,
            domain_id=domain_id,
            list_id=list_id,
        )
        if not resolved:
            raise ThingsError(f"{item_type} is required")
        return resolved

    def _resolve_optional_task_ref(self, ref: Any) -> str:
        if ref is None:
            return ""
        return self._resolve_required_ref("task", ref)

    def _replace_tags(self, item_id: str, tags: Any) -> None:
        self._connection.execute("DELETE FROM thing_tags WHERE thing_id = ?", (item_id,))
        for index, tag in enumerate(_string_list(tags)):
            self._connection.execute(
                "INSERT INTO thing_tags (thing_id, tag, position) VALUES (?, ?, ?)",
                (item_id, tag, index),
            )

    def _replace_attachments(self, item_id: str, attachments: Any) -> None:
        self._connection.execute("DELETE FROM thing_attachments WHERE thing_id = ?", (item_id,))
        for index, value in enumerate(_string_list(attachments)):
            self._connection.execute(
                "INSERT INTO thing_attachments (thing_id, value, position) VALUES (?, ?, ?)",
                (item_id, value, index),
            )

    def _replace_reminders(self, item_id: str, reminders: Any) -> None:
        self._connection.execute("DELETE FROM thing_reminders WHERE thing_id = ?", (item_id,))
        for index, reminder in enumerate(_dict_items(reminders)):
            if "before_minutes" in reminder:
                self._connection.execute(
                    """
                    INSERT INTO thing_reminders (
                        id, thing_id, kind, before_minutes, remind_at, position
                    ) VALUES (?, ?, 'before_minutes', ?, '', ?)
                    """,
                    (generate_id("rem"), item_id, int(reminder.get("before_minutes") or 0), index),
                )
            elif "at" in reminder:
                self._connection.execute(
                    """
                    INSERT INTO thing_reminders (
                        id, thing_id, kind, before_minutes, remind_at, position
                    ) VALUES (?, ?, 'at', 0, ?, ?)
                    """,
                    (generate_id("rem"), item_id, _normalize_datetime(reminder.get("at")), index),
                )
            else:
                raise ThingsError("reminder 需要 before_minutes 或 at")

    def _replace_recurrence(self, item_id: str, recurrence: Any) -> None:
        self._connection.execute("DELETE FROM thing_recurrence WHERE thing_id = ?", (item_id,))
        if recurrence in (None, {}, []):
            return
        if not isinstance(recurrence, dict):
            raise ThingsError("recurrence must be an object")
        frequency = str(recurrence.get("frequency") or "").strip()
        if frequency not in {"day", "week", "month", "year"}:
            raise ThingsError("recurrence.frequency must be day/week/month/year")
        self._validate_recurrence_anchor(item_id)
        _validate_recurrence_rule(recurrence, frequency)
        self._connection.execute(
            """
            INSERT INTO thing_recurrence (
                thing_id, frequency, interval, anchor, on_non_workday,
                ends_kind, ends_count, ends_at, rule_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                frequency,
                max(1, int(recurrence.get("interval") or 1)),
                str(recurrence.get("anchor") or "schedule"),
                str(recurrence.get("on_non_workday") or ""),
                _recurrence_ends_kind(recurrence.get("ends")),
                _recurrence_ends_count(recurrence.get("ends")),
                _recurrence_ends_at(recurrence.get("ends")),
                _json(recurrence),
            ),
        )

    def _validate_recurrence_anchor(self, item_id: str) -> None:
        item_type = str(self._thing_row(item_id)["type"])
        if item_type == "task":
            if not str(self._task_row(item_id)["scheduled_at"] or ""):
                raise ThingsError("设 recurrence 前必须先设置 scheduled_at")
        elif item_type == "event":
            if not str(self._event_row(item_id)["start_at"] or ""):
                raise ThingsError("设 recurrence 前必须先设置 start_at")

    def _replace_blocked_by(self, item_id: str, blocked_by: Any) -> None:
        self._connection.execute("DELETE FROM task_dependencies WHERE task_id = ?", (item_id,))
        for ref in _dict_items(blocked_by):
            dependency = self._resolve_required_ref("task", ref)
            if dependency == item_id:
                raise ThingsError("task 不能阻塞自己")
            self._connection.execute(
                """
                INSERT OR IGNORE INTO task_dependencies (task_id, blocked_by_task_id)
                VALUES (?, ?)
                """,
                (item_id, dependency),
            )
        self._ensure_no_dependency_cycle(item_id)

    def _detail(self, item_id: str) -> dict[str, Any]:
        row = self._thing_row(item_id)
        item_type = str(row["type"])
        payload = {
            "type": item_type,
            "id": str(row["id"]),
            "title": str(row["title"]),
            "position": int(row["position"] or 0),
            "description": _none_if_empty(row["description"]),
            "status": str(row["status"]),
            "meta": {
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
            },
        }
        if item_type != "domain":
            payload["domain"] = self._ref_payload(str(row["domain_id"]))
        if item_type not in {"domain", "list"}:
            payload["list"] = self._ref_payload(str(row["list_id"]))
        if item_type in ITEM_TYPES:
            payload["section"] = self._ref_payload(str(row["section_id"]))
            payload["attachments"] = self._attachments(item_id)
            payload["tags"] = self._tags(item_id)
            payload["reminder"] = self._reminders(item_id)
            payload["recurrence"] = self._recurrence(item_id)
            payload["meta"]["completed_at"] = _none_if_empty(row["completed_at"])
        elif item_type in {"domain", "list"}:
            payload["tags"] = self._tags(item_id)
        if item_type == "section":
            payload["list"] = self._ref_payload(str(row["list_id"]))
        if item_type == "task":
            task = self._task_row(item_id)
            payload.update(
                {
                    "parent_task": self._task_ref_payload(str(task["parent_task_id"])),
                    "blocked_by": [
                        self._task_ref_payload(str(dep["blocked_by_task_id"]))
                        for dep in self._connection.execute(
                            "SELECT blocked_by_task_id FROM task_dependencies WHERE task_id = ?",
                            (item_id,),
                        ).fetchall()
                    ],
                    "defer_until": _none_if_empty(task["defer_until"]),
                    "deadline": _none_if_empty(task["deadline_at"]),
                    "scheduled_at": _none_if_empty(task["scheduled_at"]),
                    "location": _none_if_empty(task["location"]),
                    "fixed": bool(task["fixed"]),
                    "state": self._task_state(item_id, task),
                }
            )
        elif item_type == "event":
            event = self._event_row(item_id)
            payload.update(
                {
                    "defer_until": _none_if_empty(event["defer_until"]),
                    "start_at": _none_if_empty(event["start_at"]),
                    "duration": _duration_payload(int(event["duration_minutes"] or 0)),
                    "location": _none_if_empty(event["location"]),
                    "fixed": bool(event["fixed"]),
                    "deadline": _none_if_empty(event["deadline_at"]),
                    "state": self._event_state(event),
                }
            )
        elif item_type in CONTAINER_TYPES:
            payload["stats"] = self._stats(item_id, item_type)
        return payload

    def _summary(self, item_id: str) -> dict[str, Any]:
        detail = self._detail(item_id)
        return {
            "id": detail["id"],
            "type": detail["type"],
            "title": detail["title"],
            "status": detail["status"],
        }

    def _list_items(self, data: dict[str, Any], types: list[str]) -> list[dict[str, Any]]:
        status = str(data.get("status") or "open")
        values: list[Any] = []
        sql = "SELECT things.* FROM things WHERE type IN (%s)" % ",".join("?" for _ in types)
        values.extend(types)
        if status == "completed" and (
            "scheduled_in" in data or "completed_in" in data
        ):
            sql += """
                AND (
                    status = ?
                    OR id IN (
                        SELECT thing_id FROM occurrence_overrides
                        WHERE status = 'completed'
                    )
                )
            """
            values.append(status)
        elif status == "all":
            sql += " AND status != 'deleted'"
        else:
            sql += " AND status = ?"
            values.append(status)
        sql, values = self._append_container_filters(sql, values, data)
        text = str(data.get("text") or "").strip()
        if text:
            sql += " AND title LIKE ?"
            values.append(f"%{text}%")
        rows = self._connection.execute(sql, values).fetchall()
        items = [self._list_item_from_row(row) for row in rows]
        items = self._filter_by_schedule(items, data.get("schedule"))
        items = self._filter_by_tags(items, data.get("tags"))
        items = self._expand_recurrences(items, data)
        items = self._filter_by_windows(items, data)
        sort = str(data.get("sort") or "when")
        return sorted(items, key=lambda item: _sort_key(item, sort))

    def _apply_default_list_filters(self, data: dict[str, Any], types: list[str]) -> None:
        if any(item in CONTAINER_TYPES for item in types):
            return
        if any(key in data for key in ("scheduled_in", "due_in", "completed_in", "schedule")):
            return
        if str(data.get("status") or "open") != "open":
            return
        today = user_now().date()
        window = {
            "from": today.isoformat(),
            "to": (today + timedelta(days=7)).isoformat(),
        }
        data["scheduled_in"] = dict(window)
        data["due_in"] = dict(window)
        data.setdefault("include_overdue", True)
        data.setdefault("include_anytime", True)
        data.setdefault("include_deferred", True)

    def _list_containers(self, data: dict[str, Any], types: list[str]) -> list[dict[str, Any]]:
        status = str(data.get("status") or "open")
        values: list[Any] = []
        container_types = [item for item in types if item in CONTAINER_TYPES]
        sql = "SELECT * FROM things WHERE type IN (%s)" % ",".join("?" for _ in container_types)
        values.extend(container_types)
        if status == "all":
            sql += " AND status != 'deleted'"
        else:
            sql += " AND status = ?"
            values.append(status)
        sql, values = self._append_container_filters(sql, values, data)
        text = str(data.get("text") or "").strip()
        if text:
            sql += " AND title LIKE ?"
            values.append(f"%{text}%")
        sql += " ORDER BY type ASC, position ASC, title ASC"
        rows = self._connection.execute(sql, values).fetchall()
        return [
            {
                "id": str(row["id"]),
                "type": str(row["type"]),
                "title": str(row["title"]),
                "status": str(row["status"]),
                "domain_id": str(row["domain_id"] or ""),
                "list_id": str(row["list_id"] or ""),
                "position": int(row["position"] or 0),
                "open": self._stats(str(row["id"]), str(row["type"]))["open"]["total"],
            }
            for row in rows
        ]

    def _append_container_filters(
        self,
        sql: str,
        values: list[Any],
        data: dict[str, Any],
    ) -> tuple[str, list[Any]]:
        for key, column in (("domain", "domain_id"), ("list", "list_id"), ("section", "section_id")):
            if key not in data:
                continue
            value = data.get(key)
            if value is None:
                sql += f" AND {column} = ''"
                continue
            item_type = key
            ref_id = self._resolve_optional_ref(item_type, value)
            sql += f" AND {column} = ?"
            values.append(ref_id)
        return sql, values

    def _list_item_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        item_id = str(row["id"])
        item_type = str(row["type"])
        status = str(row["status"])
        if item_type == "task":
            detail_row = self._task_row(item_id)
            when = str(detail_row["scheduled_at"] or "")
            duration = None
            end_at = None
            fixed = bool(detail_row["fixed"])
            deadline = str(detail_row["deadline_at"] or "")
            schedule = self._schedule(str(detail_row["defer_until"] or ""), when)
        else:
            detail_row = self._event_row(item_id)
            when = str(detail_row["start_at"] or "")
            duration = _duration_payload(int(detail_row["duration_minutes"] or 0))
            end_at = _end_at(when, int(detail_row["duration_minutes"] or 0))
            fixed = bool(detail_row["fixed"])
            deadline = str(detail_row["deadline_at"] or "")
            schedule = self._schedule(str(detail_row["defer_until"] or ""), when)
        location = str(detail_row["location"] or "")
        item = {
            "id": item_id,
            "type": item_type,
            "title": str(row["title"]),
            "status": status,
            "tags": self._tags(item_id),
            "position": int(row["position"] or 0),
            "_created_at": str(row["created_at"]),
            "schedule": schedule,
            "when": when or None,
            "duration": duration,
            "end_at": end_at,
            "location": location or None,
            "fixed": fixed,
            "deadline": deadline or None,
            "days_to_deadline": _days_to_deadline(deadline),
            "activates_at": _activates_at(str(detail_row["defer_until"] or "")),
            "is_blocked": item_type == "task" and self._is_blocked(item_id),
            "is_blocking": item_type == "task" and self._is_blocking(item_id),
            "occurrence_date": None,
            "domain": self._ref_payload(str(row["domain_id"])),
            "list": self._ref_payload(str(row["list_id"])),
            "section": self._ref_payload(str(row["section_id"])),
        }
        _set_item_overdue_state(item, status)
        return item

    def _filter_by_schedule(self, items: list[dict[str, Any]], value: Any) -> list[dict[str, Any]]:
        if value is None:
            return items
        allowed = {str(item) for item in (value if isinstance(value, list) else [value])}
        allowed = allowed & SCHEDULES
        return [item for item in items if item.get("schedule") in allowed] if allowed else items

    def _filter_by_tags(self, items: list[dict[str, Any]], spec: Any) -> list[dict[str, Any]]:
        if not isinstance(spec, dict):
            return items
        all_tags = set(_string_list(spec.get("all")))
        any_tags = set(_string_list(spec.get("any")))
        none_tags = set(_string_list(spec.get("none")))
        result = []
        for item in items:
            item_tags = set(self._tags(str(item["id"])))
            if all_tags and not all_tags.issubset(item_tags):
                continue
            if any_tags and not (item_tags & any_tags):
                continue
            if none_tags and (item_tags & none_tags):
                continue
            result.append(item)
        return result

    def _expand_recurrences(self, items: list[dict[str, Any]], data: dict[str, Any]) -> list[dict[str, Any]]:
        status = str(data.get("status") or "open")
        should_expand_completed = status in {"completed", "all"} and (
            "completed_in" in data or "scheduled_in" not in data
        )
        if "scheduled_in" not in data and not should_expand_completed:
            return items
        if "scheduled_in" in data:
            start, end = _window(data.get("scheduled_in"))
            if start is None and end is None:
                return items
            start, end = _bounded_window(start, end)
            expand_start = _include_overdue_start(start, data.get("include_overdue"))
        else:
            completed_start, completed_end = _window(data.get("completed_in"))
            start, end = _bounded_window(completed_start, completed_end)
            expand_start = start
        requested_status = status
        expanded: list[dict[str, Any]] = []
        for item in items:
            item_id = str(item["id"])
            recurrence = self._recurrence(item_id)
            if not recurrence:
                expanded.append(item)
                continue
            overrides = self._occurrence_overrides(item_id)
            if "scheduled_in" not in data:
                expanded.extend(
                    self._override_occurrence_items(
                        item,
                        overrides,
                        requested_status=requested_status,
                    )
                )
                continue
            item_expand_start = expand_start
            if item.get("type") == "event":
                duration = item.get("duration") if isinstance(item.get("duration"), dict) else {}
                item_expand_start = item_expand_start - timedelta(minutes=int(duration.get("minutes") or 0))
            for occurrence_time in self._recurrence_occurrences(item, item_expand_start, end):
                occurrence_date = _occurrence_key(occurrence_time)
                occurrence = dict(item)
                occurrence["when"] = occurrence_time
                occurrence["occurrence_date"] = occurrence_date
                if occurrence["type"] == "event":
                    duration = occurrence.get("duration") if isinstance(occurrence.get("duration"), dict) else {}
                    occurrence["end_at"] = _end_at(occurrence_time, int(duration.get("minutes") or 0))
                override = overrides.get(occurrence_date, {})
                occurrence_status = str(override.get("status") or item.get("status") or "open")
                occurrence["status"] = occurrence_status
                occurrence["completed_at"] = override.get("completed_at") or ""
                for key, value in dict(override.get("override") or {}).items():
                    if key == "scheduled_at" and occurrence["type"] == "task":
                        occurrence["when"] = _normalize_item_time(value, keep_date=True)[0] or occurrence["when"]
                    elif key == "start_at" and occurrence["type"] == "event":
                        occurrence["when"] = _normalize_item_time(value, keep_date=False)[0] or occurrence["when"]
                    elif key == "duration" and occurrence["type"] == "event":
                        occurrence["duration"] = _duration_payload(_duration_minutes(value))
                    elif key == "deadline":
                        occurrence["deadline"] = _normalize_deadline(value) or None
                    elif key in {"title", "location", "fixed"}:
                        occurrence[key] = value
                if occurrence["type"] == "event":
                    duration = occurrence.get("duration") if isinstance(occurrence.get("duration"), dict) else {}
                    occurrence["end_at"] = _end_at(str(occurrence.get("when") or ""), int(duration.get("minutes") or 0))
                occurrence["days_to_deadline"] = _days_to_deadline(str(occurrence.get("deadline") or ""))
                _set_item_overdue_state(occurrence, occurrence_status)
                if requested_status != "all" and occurrence_status != requested_status:
                    continue
                expanded.append(occurrence)
        return expanded

    def _override_occurrence_items(
        self,
        item: dict[str, Any],
        overrides: dict[str, dict[str, Any]],
        *,
        requested_status: str,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for occurrence_date, override in sorted(overrides.items()):
            occurrence_status = str(override.get("status") or item.get("status") or "open")
            if requested_status != "all" and occurrence_status != requested_status:
                continue
            occurrence = dict(item)
            occurrence["occurrence_date"] = occurrence_date
            occurrence["status"] = occurrence_status
            occurrence["completed_at"] = override.get("completed_at") or ""
            occurrence["when"] = _occurrence_time_from_date(str(item.get("when") or ""), occurrence_date)
            for key, value in dict(override.get("override") or {}).items():
                if key == "scheduled_at" and occurrence["type"] == "task":
                    occurrence["when"] = _normalize_item_time(value, keep_date=True)[0] or occurrence["when"]
                elif key == "start_at" and occurrence["type"] == "event":
                    occurrence["when"] = _normalize_item_time(value, keep_date=False)[0] or occurrence["when"]
                elif key == "duration" and occurrence["type"] == "event":
                    occurrence["duration"] = _duration_payload(_duration_minutes(value))
                elif key == "deadline":
                    occurrence["deadline"] = _normalize_deadline(value) or None
                elif key in {"title", "location", "fixed"}:
                    occurrence[key] = value
            if occurrence["type"] == "event":
                duration = occurrence.get("duration") if isinstance(occurrence.get("duration"), dict) else {}
                occurrence["end_at"] = _end_at(str(occurrence.get("when") or ""), int(duration.get("minutes") or 0))
            occurrence["days_to_deadline"] = _days_to_deadline(str(occurrence.get("deadline") or ""))
            _set_item_overdue_state(occurrence, occurrence_status)
            result.append(occurrence)
        return result

    def _filter_by_windows(self, items: list[dict[str, Any]], data: dict[str, Any]) -> list[dict[str, Any]]:
        windows = [
            ("scheduled_in", "when"),
            ("due_in", "deadline"),
            ("completed_in", "completed_at"),
        ]
        active_windows: list[tuple[str, str, datetime | None, datetime | None]] = []
        for key, field in windows:
            if key not in data:
                continue
            start, end = _window(data.get(key))
            if start is None and end is None:
                continue
            active_windows.append((key, field, start, end))

        if active_windows:
            result: list[dict[str, Any]] = []
            for item in items:
                matched_item: dict[str, Any] | None = None
                for key, field, start, end in active_windows:
                    matched = False
                    value = item.get(field)
                    if field == "when":
                        matched = _scheduled_item_in_window(item, start, end, data.get("include_overdue"))
                    elif field == "deadline":
                        matched = _deadline_item_in_window(item, start, end, data.get("include_overdue"))
                    elif field == "completed_at":
                        if not value:
                            value = self._thing_row(str(item["id"]))["completed_at"]
                        matched = bool(value and _time_in_window(str(value), start, end))
                    if not matched:
                        continue
                    if matched_item is None:
                        matched_item = dict(item)
                    if key == "due_in" and str(item.get("schedule") or "") in {"someday", "suspended"}:
                        matched_item["_due_in_window"] = True
                if matched_item is not None:
                    result.append(matched_item)
        else:
            result = items

        action_window = _action_window(data)
        if data.get("include_anytime") and action_window is not None:
            known = {_item_key(item) for item in result}
            for item in items:
                if (
                    _item_key(item) not in known
                    and item.get("schedule") == "anytime"
                    and item.get("status") == "open"
                    and not item.get("is_blocked")
                ):
                    result.append(item)
                    known.add(_item_key(item))
        if data.get("include_deferred") and action_window is not None:
            known = {_item_key(item) for item in result}
            for item in items:
                if _item_key(item) in known or item.get("status") != "open":
                    continue
                deferred = self._deferred_item_for_window(item, action_window)
                if deferred is not None:
                    result.append(deferred)
                    known.add(_item_key(item))
        return result

    def _deferred_item_for_window(
        self,
        item: dict[str, Any],
        window: tuple[datetime, datetime],
    ) -> dict[str, Any] | None:
        schedule = str(item.get("schedule") or "")
        if schedule not in {"someday", "suspended"}:
            return None
        start, end = window
        result: dict[str, Any] | None = None
        activates_at = str(item.get("activates_at") or "")
        if schedule == "suspended" and activates_at and _time_in_window(activates_at, start, end):
            result = dict(item)
            result["_activates_in_window"] = True
        deadline = str(item.get("deadline") or "")
        if deadline and _time_in_window(deadline, start, end):
            result = dict(result or item)
            result["_due_in_window"] = True
        return result

    def _tree_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        roots: list[dict[str, Any]] = []
        root_keys: dict[tuple[str, str], dict[str, Any]] = {}

        def node_for(
            collection: list[dict[str, Any]],
            keys: dict[tuple[str, str], dict[str, Any]],
            *,
            node_id: str,
            node_type: str,
            title: str,
        ) -> dict[str, Any]:
            key = (node_type, node_id)
            existing = keys.get(key)
            if existing is not None:
                return existing
            node = {
                "id": node_id,
                "type": node_type,
                "title": title,
                "count": 0,
                "_child_keys": {},
                "children": [],
                "items": [],
            }
            keys[key] = node
            collection.append(node)
            return node

        for item in items:
            ancestors: list[dict[str, Any]] = []
            domain = item.get("domain") if isinstance(item.get("domain"), dict) else None
            list_ref = item.get("list") if isinstance(item.get("list"), dict) else None
            section = item.get("section") if isinstance(item.get("section"), dict) else None

            parent: dict[str, Any] | None = None
            if domain:
                parent = node_for(
                    roots,
                    root_keys,
                    node_id=str(domain.get("id") or ""),
                    node_type="domain",
                    title=str(domain.get("title") or ""),
                )
                ancestors.append(parent)
            if list_ref:
                target_collection = parent["children"] if parent is not None else roots
                target_keys = parent["_child_keys"] if parent is not None else root_keys
                parent = node_for(
                    target_collection,
                    target_keys,
                    node_id=str(list_ref.get("id") or ""),
                    node_type="list",
                    title=str(list_ref.get("title") or ""),
                )
                ancestors.append(parent)
            if section:
                target_collection = parent["children"] if parent is not None else roots
                target_keys = parent["_child_keys"] if parent is not None else root_keys
                parent = node_for(
                    target_collection,
                    target_keys,
                    node_id=str(section.get("id") or ""),
                    node_type="section",
                    title=str(section.get("title") or ""),
                )
                ancestors.append(parent)
            if parent is None:
                parent = node_for(
                    roots,
                    root_keys,
                    node_id="inbox",
                    node_type="inbox",
                    title="未归属",
                )
                ancestors.append(parent)
            parent["items"].append(_public_item(item))
            for ancestor in ancestors:
                ancestor["count"] += 1

        return [_finalize_tree_node(node) for node in sorted(roots, key=_tree_node_sort_key)]

    def _thing_ref(self, item_id: str) -> ThingRef:
        row = self._thing_row(item_id)
        return ThingRef(
            id=str(row["id"]),
            type=str(row["type"]),
            title=str(row["title"]),
            status=str(row["status"]),
            domain_id=str(row["domain_id"] or ""),
            list_id=str(row["list_id"] or ""),
            section_id=str(row["section_id"] or ""),
        )

    def _thing_row(self, item_id: str) -> sqlite3.Row:
        row = self._connection.execute("SELECT * FROM things WHERE id = ?", (str(item_id),)).fetchone()
        if row is None:
            raise ThingsError(f"找不到对象: {item_id}")
        return row

    def _task_row(self, item_id: str) -> sqlite3.Row:
        row = self._connection.execute("SELECT * FROM tasks WHERE thing_id = ?", (str(item_id),)).fetchone()
        if row is None:
            raise ThingsError(f"找不到 task 扩展: {item_id}")
        return row

    def _event_row(self, item_id: str) -> sqlite3.Row:
        row = self._connection.execute("SELECT * FROM events WHERE thing_id = ?", (str(item_id),)).fetchone()
        if row is None:
            raise ThingsError(f"找不到 event 扩展: {item_id}")
        return row

    def _require_existing_id(self, item_id: str, *, item_type: str = "") -> str:
        clean = str(item_id or "").strip()
        if not clean:
            raise ThingsError("id is required")
        row = self._thing_row(clean)
        if item_type and str(row["type"]) != item_type:
            raise ThingsError(f"id {clean} 不是 {item_type}")
        return clean

    def _resolve_unique_title(
        self,
        item_type: str,
        title: str,
        *,
        domain_id: str = "",
        list_id: str = "",
    ) -> str:
        sql = "SELECT id FROM things WHERE type = ? AND title = ? AND status NOT IN ('archived', 'deleted')"
        values: list[Any] = [item_type, title]
        if domain_id:
            sql += " AND domain_id = ?"
            values.append(domain_id)
        if list_id:
            sql += " AND list_id = ?"
            values.append(list_id)
        rows = self._connection.execute(sql, values).fetchall()
        if not rows:
            raise ThingsError(f"找不到 {item_type}: {title}")
        if len(rows) > 1:
            raise ThingsError(f"{item_type} 标题不唯一: {title}")
        return str(rows[0]["id"])

    def _ref_payload(self, item_id: str) -> dict[str, str] | None:
        if not item_id:
            return None
        row = self._thing_row(item_id)
        return {"id": str(row["id"]), "title": str(row["title"])}

    def _task_ref_payload(self, item_id: str) -> dict[str, str] | None:
        if not item_id:
            return None
        row = self._thing_row(item_id)
        return {"id": str(row["id"]), "title": str(row["title"]), "status": str(row["status"])}

    def _tags(self, item_id: str) -> list[str]:
        rows = self._connection.execute(
            "SELECT tag FROM thing_tags WHERE thing_id = ? ORDER BY position ASC, tag ASC",
            (item_id,),
        ).fetchall()
        return [str(row["tag"]) for row in rows]

    def _attachments(self, item_id: str) -> list[str]:
        rows = self._connection.execute(
            "SELECT value FROM thing_attachments WHERE thing_id = ? ORDER BY position ASC",
            (item_id,),
        ).fetchall()
        return [str(row["value"]) for row in rows]

    def _reminders(self, item_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM thing_reminders WHERE thing_id = ? ORDER BY position ASC",
            (item_id,),
        ).fetchall()
        result = []
        for row in rows:
            if row["kind"] == "before_minutes":
                result.append({"before_minutes": int(row["before_minutes"] or 0)})
            else:
                result.append({"at": str(row["remind_at"])})
        return result

    def _due_reminder_candidates(self, now_dt: datetime) -> list[ThingReminderFire]:
        rows = self._connection.execute(
            """
            SELECT thing_reminders.*
            FROM thing_reminders
            JOIN things ON things.id = thing_reminders.thing_id
            WHERE things.type IN ('task', 'event') AND things.status = 'open'
            ORDER BY thing_reminders.position ASC, thing_reminders.id ASC
            """
        ).fetchall()
        candidates: list[ThingReminderFire] = []
        for reminder in rows:
            item_id = str(reminder["thing_id"])
            item = self._list_item_from_row(self._thing_row(item_id))
            if item.get("schedule") in {"someday", "suspended"}:
                continue
            candidates.extend(
                self._due_candidates_for_reminder(
                    reminder,
                    item=item,
                    now_dt=now_dt,
                )
            )
        return sorted(
            candidates,
            key=lambda item: (
                _parse_user_datetime(item.fire_at),
                item.title,
                item.reminder_id,
                item.fire_key,
            ),
        )

    def _due_candidates_for_reminder(
        self,
        reminder: sqlite3.Row,
        *,
        item: dict[str, Any],
        now_dt: datetime,
    ) -> list[ThingReminderFire]:
        reminder_id = str(reminder["id"])
        item_id = str(item["id"])
        kind = str(reminder["kind"])
        if kind == "at":
            fire_at = str(reminder["remind_at"] or "")
            if not fire_at or _parse_user_datetime(fire_at) > now_dt:
                return []
            return [
                self._thing_reminder_candidate(
                    reminder_id=reminder_id,
                    item=item,
                    fire_key=fire_at,
                    fire_at=fire_at,
                    reminder_kind=kind,
                )
            ]
        when = str(item.get("when") or "")
        if not when or len(when) == 10:
            return []
        before = max(0, int(reminder["before_minutes"] or 0))
        if self._has_recurrence(item_id):
            return self._due_recurrence_before_candidates(
                reminder_id=reminder_id,
                item=item,
                before_minutes=before,
                now_dt=now_dt,
            )
        fire_at = _format_datetime(_parse_user_datetime(when) - timedelta(minutes=before))
        if _parse_user_datetime(fire_at) > now_dt:
            return []
        return [
            self._thing_reminder_candidate(
                reminder_id=reminder_id,
                item=item,
                fire_key=fire_at,
                fire_at=fire_at,
                reminder_kind=kind,
            )
        ]

    def _due_recurrence_before_candidates(
        self,
        *,
        reminder_id: str,
        item: dict[str, Any],
        before_minutes: int,
        now_dt: datetime,
    ) -> list[ThingReminderFire]:
        start = now_dt - timedelta(days=370)
        end = now_dt + timedelta(minutes=max(0, before_minutes))
        item_id = str(item["id"])
        overrides = self._occurrence_overrides(item_id)
        result: list[ThingReminderFire] = []
        for occurrence_time in self._recurrence_occurrences(item, start, end):
            occurrence_date = _occurrence_key(occurrence_time)
            status = str(overrides.get(occurrence_date, {}).get("status") or "open")
            if status in {"completed", "archived"}:
                continue
            occurrence_dt = _parse_user_datetime(occurrence_time)
            fire_at = _format_datetime(occurrence_dt - timedelta(minutes=max(0, before_minutes)))
            if _parse_user_datetime(fire_at) > now_dt:
                continue
            result.append(
                self._thing_reminder_candidate(
                    reminder_id=reminder_id,
                    item=item,
                    fire_key=occurrence_date,
                    fire_at=fire_at,
                    reminder_kind="before_minutes",
                    occurrence_date=occurrence_date,
                    when=occurrence_time,
                )
            )
        return result

    def _thing_reminder_candidate(
        self,
        *,
        reminder_id: str,
        item: dict[str, Any],
        fire_key: str,
        fire_at: str,
        reminder_kind: str,
        occurrence_date: str = "",
        when: str = "",
    ) -> ThingReminderFire:
        domain = item.get("domain") if isinstance(item.get("domain"), dict) else {}
        list_ref = item.get("list") if isinstance(item.get("list"), dict) else {}
        section = item.get("section") if isinstance(item.get("section"), dict) else {}
        return ThingReminderFire(
            id="",
            reminder_id=reminder_id,
            thing_id=str(item["id"]),
            fire_key=str(fire_key),
            fire_at=str(fire_at),
            thing_type=str(item["type"]),
            title=str(item["title"]),
            reminder_kind=str(reminder_kind),
            occurrence_date=str(occurrence_date or ""),
            when=str(when or item.get("when") or ""),
            deadline=str(item.get("deadline") or ""),
            location=str(item.get("location") or ""),
            domain_title=str(domain.get("title") or "") if isinstance(domain, dict) else "",
            list_title=str(list_ref.get("title") or "") if isinstance(list_ref, dict) else "",
            section_title=str(section.get("title") or "") if isinstance(section, dict) else "",
        )

    def _reminder_fire_row(self, reminder_id: str, fire_key: str) -> sqlite3.Row | None:
        return self._connection.execute(
            """
            SELECT * FROM thing_reminder_fires
            WHERE reminder_id = ? AND fire_key = ?
            """,
            (str(reminder_id), str(fire_key)),
        ).fetchone()

    def _recurrence(self, item_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT rule_json FROM thing_recurrence WHERE thing_id = ?",
            (item_id,),
        ).fetchone()
        return json.loads(str(row["rule_json"])) if row else None

    def _has_recurrence(self, item_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM thing_recurrence WHERE thing_id = ?",
            (item_id,),
        ).fetchone()
        return row is not None

    def _update_occurrence(
        self,
        item_id: str,
        item_type: str,
        occurrence_date: str,
        raw: dict[str, Any],
        before: dict[str, Any],
    ) -> tuple[str, list[str]]:
        if item_type not in ITEM_TYPES:
            raise ThingsError("只有 task/event 的循环 occurrence 可以单独更新")
        if not self._has_recurrence(item_id):
            raise ThingsError("非循环事项不能用 occurrence_date 定位")
        if raw.get("type") is not None:
            raise ThingsError("单次 occurrence 不能改 type")
        allowed = {
            "target",
            "id",
            "status",
            "title",
            "description",
            "location",
            "attachments",
            "tags",
            "deadline",
            "scheduled_at",
            "start_at",
            "duration",
            "fixed",
            "reminder",
        }
        extra = sorted(set(raw) - allowed)
        if extra:
            raise ThingsError(f"这些字段不能只改单次 occurrence: {', '.join(extra)}")
        now = user_now_iso()
        status = str(raw.get("status") or "open").strip()
        if status not in {"open", "completed", "archived"}:
            raise ThingsError("occurrence status 必须是 open/completed/archived")
        if item_type == "event" and status == "completed":
            raise ThingsError("event 不能设为 completed")
        override = {
            key: raw[key]
            for key in (
                "title",
                "description",
                "location",
                "attachments",
                "tags",
                "deadline",
                "scheduled_at",
                "start_at",
                "duration",
                "fixed",
                "reminder",
            )
            if key in raw
        }
        if status == "open" and not override:
            self._connection.execute(
                "DELETE FROM occurrence_overrides WHERE thing_id = ? AND occurrence_date = ?",
                (item_id, occurrence_date),
            )
            notes = [f"已恢复 occurrence {occurrence_date}"]
        else:
            self._connection.execute(
                """
                INSERT INTO occurrence_overrides (
                    thing_id, occurrence_date, status, completed_at,
                    override_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(thing_id, occurrence_date) DO UPDATE SET
                    status = excluded.status,
                    completed_at = excluded.completed_at,
                    override_json = excluded.override_json,
                    updated_at = excluded.updated_at
                """,
                (
                    item_id,
                    occurrence_date,
                    status,
                    now if status == "completed" else "",
                    _json(override),
                    now,
                    now,
                ),
            )
            notes = [f"已更新 occurrence {occurrence_date}"]
        self._touch(item_id)
        after = self._detail(item_id)
        self._audit("update_occurrence", item_id, before, after)
        return item_id, notes

    def _occurrence_overrides(self, item_id: str) -> dict[str, dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT * FROM occurrence_overrides WHERE thing_id = ?",
            (item_id,),
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                override = json.loads(str(row["override_json"] or "{}"))
            except json.JSONDecodeError:
                override = {}
            result[str(row["occurrence_date"])] = {
                "status": str(row["status"]),
                "completed_at": str(row["completed_at"] or ""),
                "override": override if isinstance(override, dict) else {},
            }
        return result

    def _current_occurrence_date(self, item_id: str) -> str | None:
        item = self._list_item_from_row(self._thing_row(item_id))
        now = user_now()
        start = now - timedelta(days=370)
        end = now + timedelta(days=370)
        occurrences = self._recurrence_occurrences(item, start, end)
        overrides = self._occurrence_overrides(item_id)
        pending = [
            value
            for value in occurrences
            if _parse_user_datetime(value) <= now
            and overrides.get(_occurrence_key(value), {}).get("status") != "completed"
            and overrides.get(_occurrence_key(value), {}).get("status") != "archived"
        ]
        if pending:
            return _occurrence_key(max(pending, key=_parse_user_datetime))
        future = [
            value
            for value in occurrences
            if _parse_user_datetime(value) > now
            and overrides.get(_occurrence_key(value), {}).get("status") != "archived"
        ]
        if future:
            return _occurrence_key(min(future, key=_parse_user_datetime))
        return None

    def _recurrence_occurrences(
        self,
        item: dict[str, Any],
        start: datetime,
        end: datetime,
    ) -> list[str]:
        item_id = str(item["id"])
        rule = self._recurrence(item_id)
        when = str(item.get("when") or "")
        if not rule or not when:
            return []
        base, precision = _parse_date_or_datetime(when)
        if end < base:
            return []
        frequency = str(rule.get("frequency") or "")
        if frequency not in {"day", "week", "month", "year"}:
            return []
        interval = max(1, int(rule.get("interval") or 1))
        end_limit = _recurrence_end_datetime(rule, end)
        if end_limit < end:
            end = end_limit
        count_limit = _recurrence_count_limit(rule)
        results: list[str] = []
        matched_count = 0
        current = base.date()
        final_date = end.date()
        while current <= final_date:
            if _date_matches_recurrence(
                current,
                base.date(),
                frequency=frequency,
                interval=interval,
                rule=rule,
            ):
                matched_count += 1
                if count_limit and matched_count > count_limit:
                    break
                occurrence_dt = datetime.combine(current, base.time())
                if occurrence_dt >= start and occurrence_dt <= end:
                    results.append(current.isoformat() if precision == "date" else _format_datetime(occurrence_dt))
            current += timedelta(days=1)
        return results

    def _task_state(self, item_id: str, task: sqlite3.Row) -> dict[str, Any]:
        schedule = self._schedule(str(task["defer_until"] or ""), str(task["scheduled_at"] or ""))
        return {
            "schedule": schedule,
            "activates_at": _activates_at(str(task["defer_until"] or "")),
            "is_overdue": _is_deadline_overdue(str(task["deadline_at"] or ""), "open"),
            "is_blocked": self._is_blocked(item_id),
            "is_blocking": self._is_blocking(item_id),
            "sub_tasks": self._sub_tasks(item_id),
            "blocking": self._blocking(item_id),
            "next_occurrence": None,
        }

    def _event_state(self, event: sqlite3.Row) -> dict[str, Any]:
        start_at = str(event["start_at"] or "")
        duration = int(event["duration_minutes"] or 0)
        end_at = _end_at(start_at, duration)
        now = user_now()
        return {
            "schedule": self._schedule(str(event["defer_until"] or ""), start_at),
            "activates_at": _activates_at(str(event["defer_until"] or "")),
            "is_ongoing": _is_ongoing(start_at, end_at, now),
            "end_at": end_at,
            "next_occurrence": None,
        }

    def _schedule(self, defer_until: str, when: str) -> str:
        if defer_until == "someday":
            return "someday"
        if defer_until and _parse_user_datetime(defer_until) > user_now():
            return "suspended"
        return "scheduled" if when else "anytime"

    def _stats(self, item_id: str, item_type: str) -> dict[str, Any]:
        descendants, values = self._descendant_filter(item_id, item_type)
        items = [
            self._list_item_from_row(row)
            for row in self._connection.execute(
                f"SELECT * FROM things WHERE type IN ('task', 'event') AND {descendants}",
                values,
            ).fetchall()
        ]
        open_items = [item for item in items if item["status"] == "open"]
        return {
            "lists": self._direct_count("list", "domain_id", item_id) if item_type == "domain" else 0,
            "sections": self._section_count(item_id, item_type),
            "open": {
                "total": len(open_items),
                "task": len([item for item in open_items if item["type"] == "task"]),
                "event": len([item for item in open_items if item["type"] == "event"]),
                "by_schedule": {
                    name: len([item for item in open_items if item["schedule"] == name])
                    for name in ("someday", "suspended", "anytime", "scheduled")
                },
                "overdue": len([item for item in open_items if item["is_overdue"]]),
                "blocked": len([item for item in open_items if item["type"] == "task" and item["is_blocked"]]),
            },
            "completed": len([item for item in items if item["status"] == "completed"]),
            "archived": len([item for item in items if item["status"] == "archived"]),
        }

    def _descendant_filter(self, item_id: str, item_type: str) -> tuple[str, list[Any]]:
        if item_type == "domain":
            return "domain_id = ?", [item_id]
        if item_type == "list":
            return "list_id = ?", [item_id]
        return "section_id = ?", [item_id]

    def _direct_count(self, item_type: str, column: str, item_id: str) -> int:
        row = self._connection.execute(
            f"SELECT COUNT(*) AS count FROM things WHERE type = ? AND {column} = ? AND status NOT IN ('archived', 'deleted')",
            (item_type, item_id),
        ).fetchone()
        return int(row["count"] or 0)

    def _section_count(self, item_id: str, item_type: str) -> int:
        if item_type == "domain":
            row = self._connection.execute(
                "SELECT COUNT(*) AS count FROM things WHERE type = 'section' AND domain_id = ? AND status NOT IN ('archived', 'deleted')",
                (item_id,),
            ).fetchone()
            return int(row["count"] or 0)
        if item_type == "list":
            return self._direct_count("section", "list_id", item_id)
        return 0

    def _is_blocked(self, item_id: str) -> bool:
        row = self._connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM task_dependencies
            JOIN things ON things.id = task_dependencies.blocked_by_task_id
            WHERE task_dependencies.task_id = ? AND things.status = 'open'
            """,
            (item_id,),
        ).fetchone()
        return bool(row and int(row["count"] or 0))

    def _is_blocking(self, item_id: str) -> bool:
        row = self._connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM task_dependencies
            JOIN things ON things.id = task_dependencies.task_id
            WHERE task_dependencies.blocked_by_task_id = ? AND things.status = 'open'
            """,
            (item_id,),
        ).fetchone()
        return bool(row and int(row["count"] or 0))

    def _sub_tasks(self, item_id: str) -> list[dict[str, str]]:
        rows = self._connection.execute(
            """
            SELECT things.id, things.title, things.status
            FROM tasks
            JOIN things ON things.id = tasks.thing_id
            WHERE tasks.parent_task_id = ?
            ORDER BY things.created_at ASC
            """,
            (item_id,),
        ).fetchall()
        return [{"id": str(row["id"]), "title": str(row["title"]), "status": str(row["status"])} for row in rows]

    def _blocking(self, item_id: str) -> list[dict[str, str]]:
        rows = self._connection.execute(
            """
            SELECT things.id, things.title, things.status
            FROM task_dependencies
            JOIN things ON things.id = task_dependencies.task_id
            WHERE task_dependencies.blocked_by_task_id = ?
            ORDER BY things.created_at ASC
            """,
            (item_id,),
        ).fetchall()
        return [{"id": str(row["id"]), "title": str(row["title"]), "status": str(row["status"])} for row in rows]

    def _has_task_in_edges(self, item_id: str) -> bool:
        row = self._connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM tasks WHERE parent_task_id = ?) +
              (SELECT COUNT(*) FROM task_dependencies WHERE blocked_by_task_id = ?) AS count
            """,
            (item_id, item_id),
        ).fetchone()
        return bool(row and int(row["count"] or 0))

    def _ensure_no_parent_cycle(self, item_id: str) -> None:
        seen = {item_id}
        current = str(self._task_row(item_id)["parent_task_id"] or "")
        while current:
            if current in seen:
                raise ThingsError("parent_task 不能成环")
            seen.add(current)
            current = str(self._task_row(current)["parent_task_id"] or "")

    def _ensure_no_dependency_cycle(self, item_id: str) -> None:
        def visit(node: str, path: set[str]) -> None:
            if node in path:
                raise ThingsError("blocked_by 不能成环")
            rows = self._connection.execute(
                "SELECT blocked_by_task_id FROM task_dependencies WHERE task_id = ?",
                (node,),
            ).fetchall()
            for row in rows:
                visit(str(row["blocked_by_task_id"]), {node, *path})

        visit(item_id, set())

    def _touch(self, item_id: str) -> None:
        self._connection.execute(
            "UPDATE things SET updated_at = ? WHERE id = ?",
            (user_now_iso(), item_id),
        )

    def _audit(self, operation: str, item_id: str, before: dict[str, Any], after: dict[str, Any]) -> None:
        self._connection.execute(
            """
            INSERT INTO thing_audit_log (
                id, created_at, actor, operation, target_id, target_type,
                before_json, after_json, summary
            ) VALUES (?, ?, 'agent', ?, ?, ?, ?, ?, ?)
            """,
            (
                generate_id("aud"),
                user_now_iso(),
                operation,
                item_id,
                str(after.get("type") or before.get("type") or ""),
                _json(before),
                _json(after),
                f"{operation} {item_id}",
            ),
        )

    def _init_schema(self) -> None:
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS things (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  id TEXT NOT NULL UNIQUE,
                  type TEXT NOT NULL,
                  title TEXT NOT NULL,
                  description TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT 'open',
                  domain_id TEXT NOT NULL DEFAULT '',
                  list_id TEXT NOT NULL DEFAULT '',
                  section_id TEXT NOT NULL DEFAULT '',
                  position INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  completed_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS tasks (
                  thing_id TEXT PRIMARY KEY,
                  parent_task_id TEXT NOT NULL DEFAULT '',
                  defer_until TEXT NOT NULL DEFAULT '',
                  deadline_at TEXT NOT NULL DEFAULT '',
                  scheduled_at TEXT NOT NULL DEFAULT '',
                  scheduled_precision TEXT NOT NULL DEFAULT '',
                  fixed INTEGER NOT NULL DEFAULT 0,
                  location TEXT NOT NULL DEFAULT '',
                  FOREIGN KEY(thing_id) REFERENCES things(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS events (
                  thing_id TEXT PRIMARY KEY,
                  defer_until TEXT NOT NULL DEFAULT '',
                  start_at TEXT NOT NULL DEFAULT '',
                  start_precision TEXT NOT NULL DEFAULT '',
                  duration_minutes INTEGER NOT NULL DEFAULT 0,
                  fixed INTEGER NOT NULL DEFAULT 0,
                  deadline_at TEXT NOT NULL DEFAULT '',
                  location TEXT NOT NULL DEFAULT '',
                  FOREIGN KEY(thing_id) REFERENCES things(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS task_dependencies (
                  task_id TEXT NOT NULL,
                  blocked_by_task_id TEXT NOT NULL,
                  PRIMARY KEY(task_id, blocked_by_task_id),
                  FOREIGN KEY(task_id) REFERENCES tasks(thing_id) ON DELETE CASCADE,
                  FOREIGN KEY(blocked_by_task_id) REFERENCES tasks(thing_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS thing_tags (
                  thing_id TEXT NOT NULL,
                  tag TEXT NOT NULL,
                  position INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY(thing_id, tag),
                  FOREIGN KEY(thing_id) REFERENCES things(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS thing_attachments (
                  thing_id TEXT NOT NULL,
                  value TEXT NOT NULL,
                  position INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY(thing_id, value),
                  FOREIGN KEY(thing_id) REFERENCES things(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS thing_reminders (
                  id TEXT PRIMARY KEY,
                  thing_id TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  before_minutes INTEGER NOT NULL DEFAULT 0,
                  remind_at TEXT NOT NULL DEFAULT '',
                  position INTEGER NOT NULL DEFAULT 0,
                  FOREIGN KEY(thing_id) REFERENCES things(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS thing_reminder_fires (
                  id TEXT PRIMARY KEY,
                  reminder_id TEXT NOT NULL,
                  thing_id TEXT NOT NULL,
                  fire_key TEXT NOT NULL,
                  fire_at TEXT NOT NULL,
                  status TEXT NOT NULL,
                  claimed_at TEXT NOT NULL DEFAULT '',
                  completed_at TEXT NOT NULL DEFAULT '',
                  last_error TEXT NOT NULL DEFAULT '',
                  UNIQUE(reminder_id, fire_key),
                  FOREIGN KEY(reminder_id) REFERENCES thing_reminders(id) ON DELETE CASCADE,
                  FOREIGN KEY(thing_id) REFERENCES things(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS thing_recurrence (
                  thing_id TEXT PRIMARY KEY,
                  frequency TEXT NOT NULL,
                  interval INTEGER NOT NULL DEFAULT 1,
                  anchor TEXT NOT NULL DEFAULT 'schedule',
                  on_non_workday TEXT NOT NULL DEFAULT '',
                  ends_kind TEXT NOT NULL DEFAULT '',
                  ends_count INTEGER NOT NULL DEFAULT 0,
                  ends_at TEXT NOT NULL DEFAULT '',
                  rule_json TEXT NOT NULL,
                  FOREIGN KEY(thing_id) REFERENCES things(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS occurrence_overrides (
                  thing_id TEXT NOT NULL,
                  occurrence_date TEXT NOT NULL,
                  status TEXT NOT NULL,
                  completed_at TEXT NOT NULL DEFAULT '',
                  override_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(thing_id, occurrence_date),
                  FOREIGN KEY(thing_id) REFERENCES things(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS thing_external_refs (
                  thing_id TEXT NOT NULL,
                  provider TEXT NOT NULL,
                  provider_type TEXT NOT NULL,
                  external_id TEXT NOT NULL,
                  deleted_at TEXT NOT NULL DEFAULT '',
                  PRIMARY KEY(provider, external_id),
                  FOREIGN KEY(thing_id) REFERENCES things(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS thing_audit_log (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  id TEXT NOT NULL UNIQUE,
                  created_at TEXT NOT NULL,
                  actor TEXT NOT NULL,
                  operation TEXT NOT NULL,
                  target_id TEXT NOT NULL,
                  target_type TEXT NOT NULL,
                  before_json TEXT NOT NULL,
                  after_json TEXT NOT NULL,
                  summary TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_things_type_status ON things(type, status);
                CREATE INDEX IF NOT EXISTS idx_things_domain ON things(domain_id, type, status);
                CREATE INDEX IF NOT EXISTS idx_things_list ON things(list_id, type, status);
                CREATE INDEX IF NOT EXISTS idx_things_section ON things(section_id, type, status);
                CREATE INDEX IF NOT EXISTS idx_things_completed ON things(completed_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_scheduled ON tasks(scheduled_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_deadline ON tasks(deadline_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_defer ON tasks(defer_until);
                CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id);
                CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_at);
                CREATE INDEX IF NOT EXISTS idx_events_deadline ON events(deadline_at);
                CREATE INDEX IF NOT EXISTS idx_events_defer ON events(defer_until);
                CREATE INDEX IF NOT EXISTS idx_tags_tag ON thing_tags(tag);
                CREATE INDEX IF NOT EXISTS idx_thing_reminder_fires_status ON thing_reminder_fires(status, fire_at);
                CREATE INDEX IF NOT EXISTS idx_external_refs_thing ON thing_external_refs(thing_id);
                """
            )
            # 旧库没有容器排序字段时按原有 seq 初始化，避免升级后顺序突然改变。
            columns = {
                str(row["name"])
                for row in self._connection.execute("PRAGMA table_info(things)").fetchall()
            }
            if "position" not in columns:
                self._connection.execute(
                    "ALTER TABLE things ADD COLUMN position INTEGER NOT NULL DEFAULT 0"
                )
                self._connection.execute("UPDATE things SET position = seq")


def _new_thing_id(item_type: str) -> str:
    return generate_id({"domain": "dom", "list": "lst", "section": "sec", "task": "tsk", "event": "evt"}[item_type])


def _clean_type(value: Any) -> str:
    text = str(value or "").strip()
    if text not in THING_TYPES:
        raise ThingsError("type 必须是 domain/list/section/task/event")
    return text


def _clean_status(value: Any, *, item_type: str) -> str:
    text = str(value or "").strip() or "open"
    if text not in STATUSES:
        raise ThingsError("status 必须是 open/completed/archived")
    if item_type in CONTAINER_TYPES and text == "completed":
        raise ThingsError(f"{item_type} 不能设为 completed")
    if item_type == "event" and text == "completed":
        raise ThingsError("event 不能设为 completed")
    return text


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ThingsError(f"{field} is required")
    return text


def _optional_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value or "").strip()


def _dict_items(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in list(value or []) if isinstance(item, dict)]


def _string_list(value: Any) -> list[str]:
    return [text for text in (str(item or "").strip() for item in list(value or [])) if text]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _normalize_defer(value: Any) -> str:
    if value is None:
        return ""
    text = str(value or "").strip()
    if not text:
        return ""
    if text == "someday":
        return "someday"
    parsed, precision = _parse_date_or_datetime(text)
    if precision == "date":
        return parsed.date().isoformat()
    return _format_datetime(parsed)


def _normalize_deadline(value: Any) -> str:
    if value is None:
        return ""
    text = str(value or "").strip()
    if not text:
        return ""
    parsed, precision = _parse_date_or_datetime(text)
    if precision == "date":
        parsed = datetime.combine(parsed.date(), time(23, 59, 59))
    return _format_datetime(parsed)


def _normalize_datetime(value: Any) -> str:
    if value is None:
        return ""
    parsed, _precision = _parse_date_or_datetime(str(value))
    return _format_datetime(parsed)


def _normalize_item_time(value: Any, *, keep_date: bool) -> tuple[str, str]:
    if value is None:
        return "", ""
    text = str(value or "").strip()
    if not text:
        return "", ""
    parsed, precision = _parse_date_or_datetime(text)
    if precision == "date" and keep_date:
        return parsed.date().isoformat(), "date"
    return _format_datetime(parsed), precision


def _parse_date_or_datetime(value: str) -> tuple[datetime, str]:
    text = str(value or "").strip()
    if not text:
        raise ThingsError("time is empty")
    try:
        if len(text) == 10:
            return datetime.combine(date.fromisoformat(text), time()), "date"
        parsed = datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError as error:
        raise ThingsError(f"时间格式不合法: {value}") from error
    if parsed.tzinfo is not None:
        raise ThingsError("时间不能带时区")
    return parsed.replace(microsecond=0), "datetime"


def _parse_user_datetime(value: str) -> datetime:
    parsed, _precision = _parse_date_or_datetime(value)
    return parsed


def _format_datetime(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat(sep=" ")


def _duration_minutes(value: Any) -> int:
    if value in (None, "", {}):
        return 0
    if not isinstance(value, dict):
        raise ThingsError("duration must be an object")
    if "minutes" in value:
        return max(0, int(value.get("minutes") or 0))
    if "days" in value:
        return max(0, int(value.get("days") or 0)) * 24 * 60
    raise ThingsError("duration 需要 minutes 或 days")


def _duration_payload(minutes: int) -> dict[str, int] | None:
    return {"minutes": int(minutes)} if minutes else None


def _is_deferred(value: str) -> bool:
    if value == "someday":
        return True
    return bool(value and _parse_user_datetime(value) > user_now())


def _ensure_not_deferred(defer_until: str, field: str) -> None:
    if _is_deferred(str(defer_until or "")):
        raise ThingsError(f"设 {field} 前必须先清空 defer_until")


def _reject_deferred_time_fields(raw: dict[str, Any], fields: tuple[str, ...]) -> None:
    present = [field for field in fields if _has_field_value(raw.get(field))]
    if present:
        raise ThingsError(f"推迟事项不能设置这些字段: {', '.join(present)}")


def _has_field_value(value: Any) -> bool:
    return value not in (None, "", [], {})


def _type_filter(value: Any, *, default: list[str]) -> list[str]:
    if value is None:
        return default
    raw = value if isinstance(value, list) else [value]
    result = [str(item) for item in raw if str(item) in THING_TYPES]
    return result or default


def _positive_int(value: Any, *, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(1, number)


def _none_if_empty(value: Any) -> Any:
    text = str(value or "")
    return None if not text else text


def _check_or_fill(name: str, explicit: str, derived: str) -> str:
    if explicit and derived and explicit != derived:
        raise ThingsError(f"{name} 归属不一致")
    return explicit or derived


def _activates_at(defer_until: str) -> str | None:
    if not defer_until or defer_until == "someday":
        return None
    return defer_until if _parse_user_datetime(defer_until) > user_now() else None


def _end_at(start_at: str, duration_minutes: int) -> str | None:
    if not start_at:
        return None
    start = _parse_user_datetime(start_at)
    return _format_datetime(start + timedelta(minutes=max(0, int(duration_minutes or 0))))


def _is_ongoing(start_at: str, end_at: str | None, now: datetime) -> bool:
    if not start_at or not end_at:
        return False
    start = _parse_user_datetime(start_at)
    end = _parse_user_datetime(end_at)
    return start <= now <= end


def _days_to_deadline(deadline: str) -> int | None:
    if not deadline:
        return None
    return (_parse_user_datetime(deadline).date() - user_now().date()).days


def _is_deadline_overdue(deadline: str, status: str) -> bool:
    return bool(status == "open" and deadline and _parse_user_datetime(deadline) < user_now())


def _is_when_overdue(when: str, status: str) -> bool:
    if status != "open" or not when:
        return False
    if len(when) == 10:
        return date.fromisoformat(when) < user_now().date()
    return _parse_user_datetime(when) < user_now()


def _set_item_overdue_state(item: dict[str, Any], status: str) -> None:
    is_task = str(item.get("type") or "") == "task"
    item["is_overdue"] = is_task and _is_deadline_overdue(
        str(item.get("deadline") or ""),
        status,
    )
    item["scheduled_overdue"] = is_task and _is_when_overdue(
        str(item.get("when") or ""),
        status,
    )


def _reminder_claim_is_active(claimed_at: str, now_dt: datetime) -> bool:
    if not str(claimed_at or "").strip():
        return False
    return now_dt - _parse_user_datetime(claimed_at) <= timedelta(
        minutes=THING_REMINDER_CLAIM_TIMEOUT_MINUTES
    )


def _window(value: Any) -> tuple[datetime | None, datetime | None]:
    if not isinstance(value, dict):
        return None, None
    start = _window_boundary(value.get("from"), end=False)
    end = _window_boundary(value.get("to"), end=True)
    return start, end


def _window_boundary(value: Any, *, end: bool) -> datetime | None:
    if value in (None, ""):
        return None
    parsed, precision = _parse_date_or_datetime(str(value))
    if precision == "date" and end:
        return datetime.combine(parsed.date(), time(23, 59, 59))
    return parsed


def _bounded_window(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    now = user_now()
    if start is None and end is None:
        return now - timedelta(days=30), now + timedelta(days=370)
    if start is None:
        assert end is not None
        return end - timedelta(days=370), end
    if end is None:
        return start, start + timedelta(days=370)
    return start, end


def _action_window(data: dict[str, Any]) -> tuple[datetime, datetime] | None:
    starts: list[datetime] = []
    ends: list[datetime] = []
    for key in ("scheduled_in", "due_in"):
        start, end = _window(data.get(key))
        if start is None and end is None:
            continue
        bounded_start, bounded_end = _bounded_window(start, end)
        starts.append(bounded_start)
        ends.append(bounded_end)
    if not starts or not ends:
        return None
    return min(starts), max(ends)


def _include_overdue_enabled(value: Any) -> bool:
    return value is not False


def _include_overdue_start(start: datetime, value: Any) -> datetime:
    if not _include_overdue_enabled(value):
        return start
    now = user_now()
    if start.date() <= now.date():
        return min(start, now - timedelta(days=370))
    return start


def _time_in_window(value: str, start: datetime | None, end: datetime | None) -> bool:
    parsed, precision = _parse_date_or_datetime(value)
    if precision == "date":
        parsed_end = datetime.combine(parsed.date(), time(23, 59, 59))
    else:
        parsed_end = parsed
    if start is not None and parsed_end < start:
        return False
    if end is not None and parsed > end:
        return False
    return True


def _scheduled_item_in_window(
    item: dict[str, Any],
    start: datetime | None,
    end: datetime | None,
    include_overdue: Any,
) -> bool:
    when = str(item.get("when") or "")
    if not when:
        return False
    if _overdue_rolls_into_window(item, start, include_overdue):
        return True
    if item.get("type") == "event" and item.get("end_at"):
        return _time_range_overlaps(when, str(item.get("end_at") or ""), start, end)
    return _time_in_window(when, start, end)


def _deadline_item_in_window(
    item: dict[str, Any],
    start: datetime | None,
    end: datetime | None,
    include_overdue: Any,
) -> bool:
    deadline = str(item.get("deadline") or "")
    if not deadline:
        return False
    if _overdue_rolls_into_window(item, start, include_overdue):
        return True
    return _time_in_window(deadline, start, end)


def _overdue_rolls_into_window(item: dict[str, Any], start: datetime | None, include_overdue: Any) -> bool:
    if not _include_overdue_enabled(include_overdue):
        return False
    if str(item.get("status") or "") != "open":
        return False
    now = user_now()
    if start is not None and start.date() > now.date():
        return False
    return bool(item.get("scheduled_overdue") or item.get("is_overdue"))


def _time_range_overlaps(
    start_value: str,
    end_value: str,
    window_start: datetime | None,
    window_end: datetime | None,
) -> bool:
    start, start_precision = _parse_date_or_datetime(start_value)
    end, end_precision = _parse_date_or_datetime(end_value)
    if start_precision == "date":
        start = datetime.combine(start.date(), time())
    if end_precision == "date":
        end = datetime.combine(end.date(), time(23, 59, 59))
    if end < start:
        end = start
    if window_start is not None and end < window_start:
        return False
    if window_end is not None and start > window_end:
        return False
    return True


def _sort_key(item: dict[str, Any], sort: str) -> tuple[Any, ...]:
    if sort == "deadline":
        return (_sort_time(item.get("deadline")), str(item.get("title") or ""))
    if sort == "created":
        return (_sort_created_desc(item.get("_created_at")), str(item.get("title") or ""))
    return (_sort_time(item.get("when")), str(item.get("title") or ""))


def _sort_time(value: Any) -> tuple[int, datetime]:
    if not value:
        return (1, datetime.max)
    return (0, _parse_user_datetime(str(value)))


def _sort_created_desc(value: Any) -> tuple[int, float]:
    if not value:
        return (1, 0.0)
    return (0, -_parse_user_datetime(str(value)).timestamp())


def _item_key(item: dict[str, Any]) -> tuple[str, str]:
    return str(item.get("id") or ""), str(item.get("occurrence_date") or "")


def _tree_node_sort_key(node: dict[str, Any]) -> tuple[int, str]:
    order = {"domain": 0, "list": 1, "section": 2, "inbox": 9}
    return order.get(str(node.get("type") or ""), 8), str(node.get("title") or "")


def _finalize_tree_node(node: dict[str, Any]) -> dict[str, Any]:
    children = [
        _finalize_tree_node(child)
        for child in sorted(list(node.get("children") or []), key=_tree_node_sort_key)
    ]
    payload = {
        "id": str(node.get("id") or ""),
        "type": str(node.get("type") or ""),
        "title": str(node.get("title") or ""),
        "count": int(node.get("count") or 0),
    }
    if children:
        payload["children"] = children
    leaf_items = list(node.get("items") or [])
    if leaf_items:
        payload["items"] = leaf_items
    return payload


def _public_container(item: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "id": str(item.get("id") or ""),
        "type": str(item.get("type") or ""),
        "title": str(item.get("title") or ""),
        "count": int(item.get("open") or item.get("count") or 0),
        "position": int(item.get("position") or 0),
    }
    status = str(item.get("status") or "")
    if status and status != "open":
        payload["status"] = status
    for key in ("domain_id", "list_id"):
        value = str(item.get(key) or "")
        if value:
            payload[key] = value
    return payload


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "id": str(item.get("id") or ""),
        "type": str(item.get("type") or ""),
        "title": str(item.get("title") or ""),
        "position": int(item.get("position") or 0),
    }
    status = str(item.get("status") or "")
    if status and status != "open":
        payload["status"] = status
    for key in ("when", "duration", "end_at", "deadline", "activates_at", "occurrence_date", "location"):
        value = item.get(key)
        if value:
            payload[key] = value
    flags: list[str] = []
    schedule = str(item.get("schedule") or "")
    if schedule in {"anytime", "someday", "suspended"}:
        flags.append(schedule)
    if item.get("_activates_in_window"):
        flags.append("activates_in_window")
    if item.get("_due_in_window"):
        flags.append("due_in_window")
    if item.get("fixed"):
        flags.append("fixed")
    if item.get("is_overdue"):
        flags.append("deadline_overdue")
    if item.get("scheduled_overdue"):
        flags.append("scheduled_overdue")
    if item.get("is_blocked"):
        flags.append("blocked")
    if item.get("is_blocking"):
        flags.append("blocking")
    if flags:
        payload["flags"] = flags
    tags = _string_list(item.get("tags"))
    if tags:
        payload["tags"] = tags
    return payload


def _optional_occurrence_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as error:
        raise ThingsError("occurrence_date 必须是 YYYY-MM-DD") from error


def _occurrence_key(value: str) -> str:
    parsed, _precision = _parse_date_or_datetime(value)
    return parsed.date().isoformat()


def _occurrence_time_from_date(master_when: str, occurrence_date: str) -> str | None:
    if not master_when:
        return None
    base, precision = _parse_date_or_datetime(master_when)
    current_date = date.fromisoformat(occurrence_date)
    if precision == "date":
        return current_date.isoformat()
    return _format_datetime(datetime.combine(current_date, base.time()))


def _validate_recurrence_rule(rule: dict[str, Any], frequency: str) -> None:
    if rule.get("days_of_month") and rule.get("weekdays_of_month"):
        raise ThingsError("recurrence.days_of_month 和 weekdays_of_month 不能同时设置")
    if rule.get("days_of_week") and frequency not in {"week"}:
        raise ThingsError("recurrence.days_of_week 只能用于 week")
    if (rule.get("days_of_month") or rule.get("weekdays_of_month")) and frequency not in {"month", "year"}:
        raise ThingsError("recurrence.days_of_month/weekdays_of_month 只能用于 month/year")
    if rule.get("months_of_year") and frequency != "year":
        raise ThingsError("recurrence.months_of_year 只能用于 year")
    for day in [int(item) for item in list(rule.get("days_of_month") or [])]:
        if day == 0 or day < -31 or day > 31:
            raise ThingsError("days_of_month 必须是 -31..-1 或 1..31")
    for weekday in _string_list(rule.get("days_of_week")):
        if weekday not in WEEKDAY_INDEX:
            raise ThingsError(f"非法 weekday: {weekday}")
    for spec in _dict_items(rule.get("weekdays_of_month")):
        if not int(spec.get("week_number") or 0):
            raise ThingsError("weekdays_of_month.week_number 不能为 0")
        for weekday in _string_list(spec.get("weekdays")):
            if weekday not in WEEKDAY_INDEX:
                raise ThingsError(f"非法 weekday: {weekday}")
    months = [int(item) for item in list(rule.get("months_of_year") or [])]
    if any(month < 1 or month > 12 for month in months):
        raise ThingsError("months_of_year 必须在 1..12")


def _recurrence_end_datetime(rule: dict[str, Any], fallback: datetime) -> datetime:
    ends = rule.get("ends") if isinstance(rule.get("ends"), dict) else {}
    ends_at = _recurrence_ends_at(ends)
    return min(fallback, _parse_user_datetime(ends_at)) if ends_at else fallback


def _recurrence_count_limit(rule: dict[str, Any]) -> int:
    ends = rule.get("ends") if isinstance(rule.get("ends"), dict) else {}
    return _recurrence_ends_count(ends)


def _date_matches_recurrence(
    current: date,
    base: date,
    *,
    frequency: str,
    interval: int,
    rule: dict[str, Any],
) -> bool:
    if current < base:
        return False
    if frequency == "day":
        return (current - base).days % interval == 0
    if frequency == "week":
        week_delta = (_week_start(current) - _week_start(base)).days // 7
        if week_delta < 0 or week_delta % interval != 0:
            return False
        weekdays = _weekday_set(rule.get("days_of_week")) or {base.weekday()}
        return current.weekday() in weekdays
    if frequency == "month":
        month_delta = _month_delta(base, current)
        if month_delta < 0 or month_delta % interval != 0:
            return False
        return _date_matches_month_rule(current, base, rule)
    if frequency == "year":
        year_delta = current.year - base.year
        if year_delta < 0 or year_delta % interval != 0:
            return False
        months = {int(item) for item in list(rule.get("months_of_year") or []) if int(item) > 0}
        if current.month not in (months or {base.month}):
            return False
        return _date_matches_month_rule(current, base, rule)
    return False


def _week_start(value: date) -> date:
    return value - timedelta(days=value.weekday())


def _month_delta(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month)


def _weekday_set(value: Any) -> set[int]:
    return {WEEKDAY_INDEX[item] for item in _string_list(value) if item in WEEKDAY_INDEX}


def _date_matches_month_rule(current: date, base: date, rule: dict[str, Any]) -> bool:
    days_of_month = [int(item) for item in list(rule.get("days_of_month") or [])]
    if days_of_month:
        valid_days = {_month_day(current.year, current.month, spec) for spec in days_of_month}
        return current.day in valid_days
    weekday_specs = _dict_items(rule.get("weekdays_of_month"))
    if weekday_specs:
        return any(_date_matches_weekday_spec(current, spec) for spec in weekday_specs)
    return current.day == min(base.day, calendar.monthrange(current.year, current.month)[1])


def _month_day(year: int, month: int, spec: int) -> int:
    last_day = calendar.monthrange(year, month)[1]
    if spec < 0:
        return max(1, last_day + spec + 1)
    return min(max(1, spec), last_day)


def _date_matches_weekday_spec(current: date, spec: dict[str, Any]) -> bool:
    week_number = int(spec.get("week_number") or 0)
    weekdays = _weekday_set(spec.get("weekdays"))
    if current.weekday() not in weekdays:
        return False
    matches = [
        day
        for day in range(1, calendar.monthrange(current.year, current.month)[1] + 1)
        if date(current.year, current.month, day).weekday() == current.weekday()
    ]
    if week_number > 0:
        return week_number <= len(matches) and current.day == matches[week_number - 1]
    return abs(week_number) <= len(matches) and current.day == matches[week_number]


def _recurrence_ends_kind(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    if "after_count" in value:
        return "after_count"
    if "on_time" in value:
        return "on_time"
    return ""


def _recurrence_ends_count(value: Any) -> int:
    return int(value.get("after_count") or 0) if isinstance(value, dict) else 0


def _recurrence_ends_at(value: Any) -> str:
    if isinstance(value, dict) and value.get("on_time"):
        return _normalize_datetime(value.get("on_time"))
    return ""
