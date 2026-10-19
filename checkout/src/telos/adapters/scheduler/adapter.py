"""Scheduler adapter implementation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from telos.adapters.base import (
    AdapterCapabilities,
    ApprovalRequest,
    ApprovalResponse,
    ConversationRef,
    InboundTurn,
    SendResult,
)
from telos.core.xml import xml_attr, xml_text
from telos.tools.schedule.repository import ScheduleFire


class SchedulerAdapter:
    name = "scheduler"
    capabilities = AdapterCapabilities(
        max_text_chars=20_000,
        supports_buttons=False,
        supports_threads=False,
        supports_files=False,
    )

    def __init__(self, *, auto_approve_writes: bool = True):
        self.auto_approve_writes = bool(auto_approve_writes)
        self._queue: list[InboundTurn] = []
        self.sent: list[tuple[ConversationRef, str]] = []

    def enqueue_schedule_fire(self, fire: ScheduleFire, *, ref: ConversationRef) -> InboundTurn:
        task = fire.task
        return self.enqueue_system_reminder_turn(
            ref=ref,
            text="<scheduleFireTrigger />",
            raw_event_id=f"{task.id}:{fire.fire_key}" if fire.fire_key else task.id,
            message_from="scheduler",
            system_reminders=[
                {
                    "source": "schedule",
                    "content": render_schedule_fire_reminder_content(fire),
                }
            ],
        )

    def enqueue_system_reminder_turn(
        self,
        *,
        ref: ConversationRef,
        text: str,
        raw_event_id: str,
        message_from: str,
        system_reminders: list[dict[str, str]],
    ) -> InboundTurn:
        turn = InboundTurn(
            ref=ref,
            text=str(text or "").strip(),
            raw_event_id=str(raw_event_id or ""),
            metadata={
                "synthetic": True,
                "message_from": str(message_from or "scheduler").strip() or "scheduler",
                "system_reminders": [dict(item) for item in system_reminders],
            },
        )
        self._queue.append(turn)
        return turn

    async def receive(self) -> AsyncIterator[InboundTurn]:
        while self._queue:
            yield self._queue.pop(0)

    def drain_recorded_inputs(self) -> list[InboundTurn]:
        return []

    async def send_text(self, ref: ConversationRef, text: str) -> SendResult:
        self.sent.append((ref, text))
        return SendResult(ok=True)

    async def request_approval(
        self,
        ref: ConversationRef,
        request: ApprovalRequest,
    ) -> ApprovalResponse:
        del ref, request
        if not self.auto_approve_writes:
            return ApprovalResponse(decision="deny", feedback="scheduler cannot approve writes")
        return ApprovalResponse(
            decision="approve",
            feedback="approved by scheduler",
            scope="scheduler",
        )

    async def upload_file(self, ref: ConversationRef, path: Path) -> SendResult:
        del ref
        return SendResult(ok=False, error=f"Scheduler upload is not implemented: {path}")


def render_schedule_fire_reminder_content(fire: ScheduleFire) -> str:
    task = fire.task
    return (
        "定时任务已触发。请根据任务 prompt 和当前上下文告知用户。\n"
        f'<scheduleFire taskId="{xml_attr(task.id)}" '
        f'executionMode="{xml_attr(task.execution_mode)}" '
        f'recurring="{str(task.recurring).lower()}" '
        f'stale="{str(fire.stale).lower()}" '
        f'coalescedCount="{int(fire.coalesced_count or 1)}" '
        f'scheduledAt="{xml_attr(fire.scheduled_at)}" '
        f'fireAt="{xml_attr(fire.fire_at)}">\n'
        f"<prompt>{xml_text(task.prompt)}</prompt>\n"
        "</scheduleFire>"
    )


