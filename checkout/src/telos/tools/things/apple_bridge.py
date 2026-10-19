"""Apple import/export bridge for Things."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Protocol

from telos.tools.things.repository import ThingsRepository


class AppleThingsClient(Protocol):
    def list_items(self, filters: dict[str, Any] | None = None) -> dict[str, Any]: ...
    def create(self, items: list[dict[str, Any]]) -> dict[str, Any]: ...
    def update(self, updates: list[dict[str, Any]]) -> dict[str, Any]: ...


def import_apple_items(
    repository: ThingsRepository,
    client: AppleThingsClient,
    *,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = client.list_items(dict(filters or {}))
    imported: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    list_map: dict[str, str] = {}
    section_map: dict[str, str] = {}

    for item in _list_payload(payload, "reminder_lists"):
        result = _import_one(repository, "list", _apple_list_to_thing(item), item, "reminder_list")
        _collect_import(result, imported, failed)
        if result.get("id"):
            list_map[_item_id(item)] = str(result["id"])

    for item in _list_payload(payload, "sections"):
        apple_list_id = str(item.get("list_id") or "")
        thing_list_id = list_map.get(apple_list_id)
        if not thing_list_id:
            failed.append({"item": item, "error": "section 缺少已导入的 list"})
            continue
        result = _import_one(
            repository,
            "section",
            _apple_section_to_thing(item, thing_list_id),
            item,
            "section",
        )
        _collect_import(result, imported, failed)
        if result.get("id"):
            section_map[_item_id(item)] = str(result["id"])

    for item in _list_payload(payload, "reminders") + _list_payload(payload, "tasks"):
        apple_list_id = str(item.get("list_id") or "")
        apple_section_id = str(item.get("section_id") or "")
        result = _import_one(
            repository,
            "task",
            _apple_reminder_to_thing(
                item,
                list_id=list_map.get(apple_list_id, ""),
                section_id=section_map.get(apple_section_id, ""),
            ),
            item,
            "reminder",
        )
        _collect_import(result, imported, failed)

    for item in _list_payload(payload, "events"):
        result = _import_one(repository, "event", _apple_event_to_thing(item), item, "event")
        _collect_import(result, imported, failed)

    return {"imported": imported, "failed": failed}


def export_apple_items(
    repository: ThingsRepository,
    client: AppleThingsClient,
    *,
    ids: list[str],
) -> dict[str, Any]:
    exported: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for thing_id in [str(item or "").strip() for item in ids if str(item or "").strip()]:
        try:
            detail_payload = repository.detail([{"id": thing_id}])
            detail = dict(detail_payload["items"][0])
            if detail["type"] == "task":
                apple_payload = _thing_task_to_apple(detail)
                provider_type = "reminder"
            elif detail["type"] == "event":
                apple_payload = _thing_event_to_apple(detail)
                provider_type = "event"
            else:
                failed.append({"id": thing_id, "error": "只支持导出 task/event"})
                continue
            existing = repository.external_refs_for(thing_id, provider="apple")
            apple_ref = next(
                (item for item in existing if item.get("provider_type") == provider_type),
                None,
            )
            if apple_ref:
                update_payload = {"id": apple_ref["external_id"], **apple_payload}
                client.update([update_payload])
                external_id = str(apple_ref["external_id"])
                action = "updated"
            else:
                created = client.create([{**apple_payload, "type": provider_type}])
                created_items = _list_payload(created, "created")
                if not created_items:
                    raise RuntimeError("Apple create 没有返回 created")
                external_id = _item_id(created_items[0])
                repository.import_external_ref(
                    thing_id=thing_id,
                    provider="apple",
                    provider_type=provider_type,
                    external_id=external_id,
                )
                action = "created"
            exported.append(
                {
                    "id": thing_id,
                    "type": detail["type"],
                    "provider": "apple",
                    "provider_type": provider_type,
                    "external_id": external_id,
                    "action": action,
                }
            )
        except Exception as error:  # noqa: BLE001 - normalize tool boundary failures.
            failed.append({"id": thing_id, "error": str(error)})
    return {"exported": exported, "failed": failed}


def _import_one(
    repository: ThingsRepository,
    thing_type: str,
    payload: dict[str, Any],
    apple_item: dict[str, Any],
    provider_type: str,
) -> dict[str, Any]:
    external_id = _item_id(apple_item)
    if not external_id:
        return {"error": "Apple item 缺少 id"}
    existing = repository.external_ref("apple", external_id)
    if existing:
        update = {"target": {"id": existing["thing_id"]}, **payload}
        result = repository.update([update])
        if result["failed"]:
            return {"error": result["failed"][0]["error"], "item": apple_item}
        thing_id = str(existing["thing_id"])
        action = "updated"
    else:
        result = repository.create([{**payload, "type": thing_type}])
        if result["failed"]:
            return {"error": result["failed"][0]["error"], "item": apple_item}
        thing_id = str(result["created"][0]["id"])
        action = "created"
    repository.import_external_ref(
        thing_id=thing_id,
        provider="apple",
        provider_type=provider_type,
        external_id=external_id,
    )
    return {"id": thing_id, "type": thing_type, "external_id": external_id, "action": action}


def _collect_import(result: dict[str, Any], imported: list[dict[str, Any]], failed: list[dict[str, Any]]) -> None:
    if result.get("error"):
        failed.append(result)
    else:
        imported.append(result)


def _apple_list_to_thing(item: dict[str, Any]) -> dict[str, Any]:
    return {"title": str(item.get("title") or "Apple List")}


def _apple_section_to_thing(item: dict[str, Any], list_id: str) -> dict[str, Any]:
    return {
        "title": str(item.get("title") or "Apple Section"),
        "list": {"id": list_id},
    }


def _apple_reminder_to_thing(item: dict[str, Any], *, list_id: str, section_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "title": str(item.get("title") or "Apple Reminder"),
        "description": str(item.get("notes") or ""),
        "deadline": str(item.get("due") or ""),
        "status": "completed" if bool(item.get("completed")) else "open",
        "tags": [str(tag) for tag in list(item.get("tags") or []) if str(tag)],
    }
    if section_id:
        payload["section"] = {"id": section_id}
    elif list_id:
        payload["list"] = {"id": list_id}
    return _drop_empty(payload)


def _apple_event_to_thing(item: dict[str, Any]) -> dict[str, Any]:
    start_at = str(item.get("start_at") or "")
    end_at = str(item.get("end_at") or "")
    return _drop_empty(
        {
            "title": str(item.get("title") or "Apple Event"),
            "description": str(item.get("notes") or ""),
            "start_at": start_at,
            "duration": _duration_from_range(start_at, end_at),
            "location": str(item.get("location") or ""),
        }
    )


def _thing_task_to_apple(detail: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "title": detail.get("title"),
        "notes": detail.get("description") or "",
        "due": detail.get("deadline") or detail.get("scheduled_at") or "",
        "completed": detail.get("status") == "completed",
        "tags": detail.get("tags") or [],
    }
    return _drop_empty(payload)


def _thing_event_to_apple(detail: dict[str, Any]) -> dict[str, Any]:
    start_at = str(detail.get("start_at") or "")
    duration = detail.get("duration") if isinstance(detail.get("duration"), dict) else {}
    minutes = int(duration.get("minutes") or 0)
    return _drop_empty(
        {
            "title": detail.get("title"),
            "notes": detail.get("description") or "",
            "start_at": start_at,
            "end_at": _add_minutes(start_at, minutes),
            "location": detail.get("location") or "",
        }
    )


def _duration_from_range(start_at: str, end_at: str) -> dict[str, int] | None:
    if not start_at or not end_at:
        return None
    try:
        start = datetime.fromisoformat(start_at.replace(" ", "T"))
        end = datetime.fromisoformat(end_at.replace(" ", "T"))
    except ValueError:
        return None
    minutes = max(0, int((end - start).total_seconds() // 60))
    return {"minutes": minutes} if minutes else None


def _add_minutes(start_at: str, minutes: int) -> str:
    if not start_at:
        return ""
    start = datetime.fromisoformat(start_at.replace(" ", "T"))
    return (start + timedelta(minutes=max(0, int(minutes or 0)))).isoformat(sep=" ")


def _list_payload(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    return [dict(item) for item in list(value or []) if isinstance(item, dict)]


def _item_id(item: dict[str, Any]) -> str:
    return str(
        item.get("id")
        or item.get("calendar_item_identifier")
        or ""
    ).strip()


def _drop_empty(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if value not in (None, "", [], {})
    }
