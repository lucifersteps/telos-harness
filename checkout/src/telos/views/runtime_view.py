"""Query layer over the turn runtime for a future UI.

A UI (or its HTTP API) talks only to this view, never to ``runs.sqlite`` or the
in-memory registries directly, so the internal storage can change without
touching the UI. It surfaces: the list of active turns, a turn's run record and
its low-frequency event history (``after_seq`` paged), a live attach for
streaming an in-flight turn, per-session run history, and the unified cancel.
"""

from __future__ import annotations

from dataclasses import dataclass

from telos.runtime.live_stream import LiveAttachment, LiveSnapshot
from telos.runtime.run_events import RunEvent, TurnRun
from telos.runtime.turn_runtime import TurnRuntime


@dataclass(frozen=True)
class RuntimeView:
    runtime: TurnRuntime

    def list_active_turns(self) -> list[TurnRun]:
        return self.runtime.run_events.list_active_turns()

    def get_turn_run(self, turn_id: str) -> TurnRun | None:
        return self.runtime.run_events.get_turn_run(turn_id)

    def get_turn_events(
        self, turn_id: str, *, after_seq: int = 0, limit: int = 500
    ) -> list[RunEvent]:
        return self.runtime.run_events.get_events(
            turn_id, after_seq=after_seq, limit=limit
        )

    def get_session_runs(self, session_id: str, *, limit: int = 50) -> list[TurnRun]:
        return self.runtime.run_events.list_session_runs(session_id, limit=limit)

    def attach_turn(self, turn_id: str, *, after_seq: int = 0) -> LiveAttachment | None:
        """Attach to an in-flight turn's live stream.

        Returns ``None`` if the turn is not currently streaming (finished or
        unknown); callers then fall back to :meth:`get_turn_events` for the
        durable low-frequency history.
        """
        return self.runtime.live.attach(turn_id, after_seq=after_seq)

    def snapshot_turn(self, turn_id: str, *, after_seq: int = 0) -> LiveSnapshot | None:
        """Read an in-flight turn's compacted live stream without subscribing."""
        return self.runtime.live.snapshot(turn_id, after_seq=after_seq)

    def cancel_turn(
        self,
        turn_id: str,
        *,
        force: bool = True,
        preserve: bool = False,
        include_background: bool = False,
    ) -> bool:
        return self.runtime.request_cancel(
            turn_id,
            force=force,
            preserve=preserve,
            include_background=include_background,
        )

    def cancel_tool_call(self, tool_call_id: str, *, turn_id: str = "") -> bool:
        return self.runtime.request_tool_call_cancel(tool_call_id, turn_id=turn_id)

    def list_cancel_tree_turn_ids(
        self,
        turn_id: str,
        *,
        include_background: bool = False,
    ) -> list[str]:
        return [
            run.turn_id
            for run in self.runtime.run_events.list_cancel_tree_runs(
                turn_id,
                include_background=include_background,
            )
        ]

    def describe_cancel_tree(
        self,
        turn_id: str,
        *,
        include_background: bool = False,
    ) -> dict:
        return self.runtime.run_events.describe_cancel_tree(
            turn_id,
            include_background=include_background,
        )
