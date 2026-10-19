"""Schedule runtime events and context reminders."""

from __future__ import annotations

from html import escape
from typing import Any

from telos.agent.context.system_reminder import SystemReminder, SystemReminderContext
from telos.memory_space.runtime import RuntimeStateStore, RuntimeWireEvent, RuntimeWireLedger


DIRECT_MESSAGE_EVENT_TYPE = "schedule.directMessage.sent"
DIRECT_MESSAGE_CURSOR_STATE_KEY = "schedule_direct_message_cursors"


def direct_message_event_data(
    *,
    task_id: str,
    content: str,
    sent_at: str,
    delivery: dict[str, Any] | None = None,
    execution_mode: str = "directMessage",
) -> dict[str, Any]:
    return {
        "taskId": str(task_id or ""),
        "executionMode": str(execution_mode or "directMessage"),
        "content": str(content or ""),
        "sentAt": str(sent_at or ""),
        "delivery": dict(delivery or {}),
    }


class DirectMessageReminderProvider:
    def __init__(
        self,
        *,
        ledger: RuntimeWireLedger,
        state_store: RuntimeStateStore,
    ):
        self.ledger = ledger
        self.state_store = state_store
        self._pending_event_ids_by_session: dict[str, list[str]] = {}

    def collect(self, context: SystemReminderContext) -> list[SystemReminder]:
        messages = pending_direct_message_events(
            self.ledger,
            self.state_store,
            session_id=context.session_id,
        )
        self._pending_event_ids_by_session[context.session_id] = [
            message.id for message in messages
        ]
        content = render_direct_message_reminder_content(messages)
        if not content:
            return []
        return [SystemReminder(content=content, source="direct_message")]

    def mark_consumed(self, session_id: str) -> None:
        event_ids = self._pending_event_ids_by_session.pop(str(session_id or ""), [])
        mark_direct_messages_consumed(
            self.state_store,
            session_id=session_id,
            event_ids=event_ids,
        )


def render_direct_message_reminder_content(messages: list[RuntimeWireEvent]) -> str:
    if not messages:
        return ""
    lines = [
        f"本轮对话前，用户共收到了{len(messages)}条来自schedule的directMessage提醒",
    ]
    for record in messages:
        data = record.data
        task_id = escape(str(data.get("taskId") or ""), quote=True)
        sent_at = escape(str(data.get("sentAt") or record.timestamp or ""), quote=True)
        content = escape(str(data.get("content") or ""), quote=False)
        lines.append(
            f'<directMessage id="{task_id}" time="{sent_at}">{content}</directMessage>'
        )
    return "\n".join(lines)


def pending_direct_message_events(
    ledger: RuntimeWireLedger,
    state_store: RuntimeStateStore,
    *,
    session_id: str,
) -> list[RuntimeWireEvent]:
    events = _direct_message_events_for_session(ledger, session_id=session_id)
    cursor = _direct_message_cursor(state_store, session_id=session_id)
    if not cursor:
        return events
    for index, event in enumerate(events):
        if event.id == cursor:
            return events[index + 1 :]
    return events


def mark_direct_messages_consumed(
    state_store: RuntimeStateStore,
    *,
    session_id: str,
    event_ids: list[str],
) -> None:
    clean_session = str(session_id or "").strip()
    clean_ids = [
        str(event_id or "").strip()
        for event_id in event_ids
        if str(event_id or "").strip()
    ]
    if not clean_session or not clean_ids:
        return
    state = state_store.read()
    cursors = state.get(DIRECT_MESSAGE_CURSOR_STATE_KEY)
    cursors = dict(cursors) if isinstance(cursors, dict) else {}
    cursors[clean_session] = clean_ids[-1]
    state[DIRECT_MESSAGE_CURSOR_STATE_KEY] = cursors
    state_store.write(state)


def _direct_message_events_for_session(
    ledger: RuntimeWireLedger,
    *,
    session_id: str,
) -> list[RuntimeWireEvent]:
    clean_session = str(session_id or "").strip()
    return [
        event
        for event in ledger.read()
        if event.type == DIRECT_MESSAGE_EVENT_TYPE
        and event.session_id == clean_session
        and str(event.data.get("executionMode") or "") == "directMessage"
        and str(event.data.get("content") or "").strip()
    ]


def _direct_message_cursor(
    state_store: RuntimeStateStore,
    *,
    session_id: str,
) -> str:
    cursors = state_store.read().get(DIRECT_MESSAGE_CURSOR_STATE_KEY)
    if not isinstance(cursors, dict):
        return ""
    return str(cursors.get(str(session_id or "").strip()) or "").strip()
