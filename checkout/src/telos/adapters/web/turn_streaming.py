"""Web live-delta *overlay* forwarding — the ephemeral tier of a two-tier model.

Delivery to the browser is split into two tiers with disjoint responsibilities:

1. **Canonical (authoritative) tier** — the per-session conversation event log,
   delivered elsewhere as ``conversation.event`` (see ``_watch_conversation_events``
   in ``server.py``). It is globally ordered by a monotonic session ``seq``,
   idempotent, and carries stable identity on every event (``tool_record_id`` /
   ``approval_id``). It is the *only* source the client builds its timeline from:
   user messages, ``assistant.message.completed`` (final text),
   ``assistant.message.interrupted`` (stopped partial text), tool lifecycle,
   approvals, and ``turn.*`` lifecycle. History, live, and reconnect all flow
   through it (catch-up by ``after_seq``, zero overlap).

2. **Overlay (ephemeral) tier** — token-level deltas that animate the
   *currently-open* message: ``llm.text.delta`` / ``llm.thinking.delta`` and the
   compacted open-message ``llm.message.snapshot`` (for mid-turn attach). These
   carry no session ``seq`` and are NOT canonical; the client applies them to a
   per-turn streaming buffer that is superseded the moment the canonical
   ``assistant.message.completed`` / ``assistant.message.interrupted`` or the
   terminal event for that turn arrives.

This module owns tier 2 only. There is exactly ONE delta producer per connection
(``forward_live_deltas``), so a turn's deltas are forwarded once — no racing
watchers, no snapshot-replays-what-was-streamed, no client-side dedup guessing.
``run_turn`` runs a send-path turn to completion (for status bookkeeping) but no
longer streams anything itself; the overlay forwarder + canonical log cover the UI.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from telos.runtime.live_stream import LAGGED_TYPE
from telos.runtime.run_events import run_depth, run_has_background_ancestor
from telos.views.runtime_view import RuntimeView

SendJson = Callable[[dict[str, Any]], Awaitable[None]]
EntryFactory = Callable[[], Awaitable[Any]]
# result -> (turn_id, final_text, aborted)
ExtractFinal = Callable[[Any], tuple[str, str, bool]]

_ATTACH_POLL_INTERVAL = 0.05
_WATCH_POLL_INTERVAL = 0.1

#: The only event types forwarded on the overlay tier. Everything else (tool
#: lifecycle, finalized assistant messages, ``turn.*``) is canonical and
#: reaches the client via the conversation event log.
_OVERLAY_TYPES = frozenset(
    {"llm.text.delta", "llm.thinking.delta", "llm.message.snapshot"}
)


@dataclass
class TurnStreamControl:
    turn_id: str = ""
    cancel_requested: bool = False


def websocket_server_event(event_type: str, **payload: Any) -> dict[str, Any]:
    return {"type": "server_event", "event": {"type": event_type, **payload}}


def websocket_runtime_event(record: dict[str, Any]) -> dict[str, Any]:
    return websocket_server_event("runtime.event", event=dict(record))


def _run_message_id(run: Any | None) -> str:
    if run is None:
        return ""
    metadata = dict(getattr(run, "metadata", {}) or {})
    return str(metadata.get("conversation_message_id") or "").strip()


async def run_send_path_turn(
    *,
    entry: EntryFactory,
    session_id: str,
    runtime_view: RuntimeView,
    send_json: SendJson,
    extract_final: ExtractFinal,
    control: TurnStreamControl | None = None,
) -> tuple[str, str, bool] | None:
    """Run a send-path turn to completion and return its final ``(turn_id, text,
    aborted)`` for status bookkeeping. Does NOT stream — the overlay forwarder
    animates deltas and the canonical conversation log delivers lifecycle/content.

    Returns ``None`` if the entry raised (a ``ui.error`` is emitted). The turn's
    ``turn.started`` / ``turn.completed`` / ``turn.failed`` reach the client via
    the canonical channel, not from here.
    """
    task: asyncio.Task[Any] = asyncio.create_task(entry())

    # Capture the turn_id as soon as the turn opens so a cancel requested at/near
    # start can act on it. This is bounded by the turn actually starting.
    turn_id = ""
    while not turn_id and not task.done():
        turn_id = active_turn_id(runtime_view, session_id)
        if turn_id:
            break
        await asyncio.sleep(_ATTACH_POLL_INTERVAL)

    if turn_id and control is not None:
        control.turn_id = turn_id
        if control.cancel_requested:
            runtime_view.cancel_turn(turn_id)
            await send_json(
                websocket_server_event("turn.cancel_requested", turn_id=turn_id)
            )

    try:
        result = await task
    except Exception as error:  # noqa: BLE001
        await send_json(websocket_server_event("ui.error", message=str(error)))
        return None

    final_turn_id, text, aborted = extract_final(result)
    return final_turn_id or turn_id, text, aborted


async def forward_live_deltas(
    *,
    session_id_fn: Callable[[], str],
    runtime_view: RuntimeView,
    send_json: SendJson,
    streamed: set[str],
) -> None:
    """The connection's single overlay producer.

    Polls the *current* active session's in-flight turns and, for each turn not
    yet attached, spawns a coroutine that forwards only ``_OVERLAY_TYPES`` deltas.
    ``session_id_fn()`` is re-resolved each round so ``/new`` and ``/reset``
    session rotation is followed. ``streamed`` de-dups by ``turn_id`` so a turn is
    attached once. Foreground child (sub-agent) turns are forwarded with a
    ``tool.call.child_started`` prefix so the client nests their deltas under the
    parent tool card *before* deltas arrive; background sub-agent turns are skipped.
    Per-turn forwarders are owned by this coroutine and cancelled before it exits.
    """
    forwarders: set[asyncio.Task[None]] = set()
    try:
        while True:
            completed = {task for task in forwarders if task.done()}
            if completed:
                await asyncio.gather(*completed, return_exceptions=True)
                forwarders.difference_update(completed)

            session_id = session_id_fn()
            runs = runtime_view.list_active_turns()
            by_turn_id = _runs_by_turn_id(runs)
            for run in _ordered_runs(runs, by_turn_id):
                turn_id = str(getattr(run, "turn_id", "") or "")
                # Follow the turn TREE, not the session: a delegated child (e.g. an
                # assistant) runs in its OWN session but is a foreground descendant
                # of a turn in this surface's session. Its content lives only in that
                # child session, so the parent surface's canonical channel never sees
                # it — we must bridge it here.
                if turn_id in streamed or not _turn_in_surface_tree(
                    run,
                    by_turn_id,
                    session_id,
                ):
                    continue
                is_child = bool(run.parent_turn_id)
                if is_child and run_has_background_ancestor(run, by_turn_id):
                    continue
                cross_session = str(getattr(run, "session_id", "") or "") != session_id
                prefix = tuple(_child_started_prefix_events(run)) if is_child else ()
                streamed.add(turn_id)
                forwarders.add(
                    asyncio.create_task(
                        _forward_turn_deltas(
                            turn_id=turn_id,
                            runtime_view=runtime_view,
                            send_json=send_json,
                            prefix_events=prefix,
                            cross_session=cross_session,
                        )
                    )
                )
            await asyncio.sleep(_WATCH_POLL_INTERVAL)
    finally:
        for task in forwarders:
            if not task.done():
                task.cancel()
        if forwarders:
            await asyncio.gather(*forwarders, return_exceptions=True)


async def _forward_turn_deltas(
    *,
    turn_id: str,
    runtime_view: RuntimeView,
    send_json: SendJson,
    prefix_events: tuple[dict[str, Any], ...] = (),
    cross_session: bool = False,
) -> None:
    """Attach to a turn's live stream and forward its records to END.

    Same-session turns forward only ``_OVERLAY_TYPES`` deltas — their tool
    lifecycle / completed / terminal are canonical and reach the client via the
    conversation event log, so forwarding them here would duplicate. A
    ``cross_session`` turn (a delegated child running in another session, e.g. an
    assistant) is NOT covered by the parent surface's canonical channel, so this
    is its ONLY source: we forward the FULL runtime stream (deltas + tool lifecycle
    + finalized assistant message + terminal). No duplication, because canonical
    carries none of it.

    The attach snapshot's compacted open-message (``llm.message.snapshot``) paints
    the current text for mid-turn attach; subsequent records animate the rest. A
    ``stream.lagged`` sentinel ends this turn's forwarding.
    """
    attachment = runtime_view.attach_turn(turn_id, after_seq=0)
    if attachment is None:
        return
    for event in prefix_events:
        await send_json(websocket_runtime_event(event))
    message_id = _run_message_id(runtime_view.get_turn_run(turn_id))
    for record in attachment.snapshot:
        await _forward_record(record, send_json, message_id, full=cross_session)
    while True:
        record = await attachment.queue.get()
        if record is None:  # END sentinel
            break
        await _forward_record(record, send_json, message_id, full=cross_session)
        if record.get("type") == LAGGED_TYPE:  # dropped as a slow consumer
            break


async def _forward_record(
    record: dict[str, Any],
    send_json: SendJson,
    message_id: str,
    *,
    full: bool,
) -> None:
    rtype = record.get("type")
    if rtype == LAGGED_TYPE:
        return  # internal sentinel; ends forwarding, never sent to the client
    if not full and rtype not in _OVERLAY_TYPES:
        return
    if message_id and not record.get("message_id"):
        record = {**record, "message_id": message_id}
    await send_json(websocket_runtime_event(record))


def _turn_in_surface_tree(
    run: Any,
    by_turn_id: dict[str, Any],
    session_id: str,
) -> bool:
    """True if ``run`` belongs to the surface's conversation tree — i.e. it is in
    ``session_id`` itself, or a (foreground) descendant of an active turn that is.

    Walks ``parent_turn_id`` over the shared active-turn set. This is what lets a
    cross-session delegated child (assistant) attach to the parent surface: its parent
    (the delegating main turn) is active and in ``session_id``.
    """
    current: Any = run
    seen: set[str] = set()
    while current is not None:
        turn_id = str(getattr(current, "turn_id", "") or "")
        if turn_id in seen:
            break
        seen.add(turn_id)
        if str(getattr(current, "session_id", "") or "") == session_id:
            return True
        parent_id = str(getattr(current, "parent_turn_id", "") or "")
        current = by_turn_id.get(parent_id) if parent_id else None
    return False


def _runs_by_turn_id(runs: list[Any]) -> dict[str, Any]:
    return {str(getattr(run, "turn_id", "") or ""): run for run in runs}


def _ordered_runs(runs: list[Any], by_turn_id: dict[str, Any]) -> list[Any]:
    # Shallower turns first so a parent is attached (and its child-started prefix
    # sent) before its children.
    return sorted(
        runs,
        key=lambda run: (
            run_depth(run, by_turn_id),
            str(getattr(run, "started_at", "") or ""),
            str(getattr(run, "turn_id", "") or ""),
        ),
    )


def _child_started_prefix_events(run: Any) -> list[dict[str, Any]]:
    parent_turn_id = str(getattr(run, "parent_turn_id", "") or "")
    parent_tool_call_id = str(getattr(run, "parent_tool_call_id", "") or "")
    child_turn_id = str(getattr(run, "turn_id", "") or "")
    if not parent_turn_id or not parent_tool_call_id or not child_turn_id:
        return []
    metadata = dict(getattr(run, "metadata", {}) or {})
    return [
        {
            "type": "tool.call.child_started",
            "turn_id": parent_turn_id,
            "tool_call_id": parent_tool_call_id,
            "child_turn_id": child_turn_id,
            "child_session_id": str(getattr(run, "session_id", "") or ""),
            "child_agent_id": str(getattr(run, "agent_id", "") or ""),
            "child_kind": str(
                metadata.get("child_kind") or metadata.get("profile_name") or ""
            ),
            "tool_name": str(metadata.get("parent_tool_name") or "agent"),
            "seq": 0,
        }
    ]


def active_turn_id(
    runtime_view: RuntimeView,
    session_id: str,
    *,
    include_child_turns: bool = False,
) -> str:
    for run in runtime_view.list_active_turns():
        if run.session_id != session_id:
            continue
        if include_child_turns or not run.parent_turn_id:
            return run.turn_id
    return ""
