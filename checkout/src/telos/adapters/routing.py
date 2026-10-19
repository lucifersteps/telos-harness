"""Runtime routing state for proactive turns."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from telos.adapters.base import ConversationRef
from telos.core.clock import user_now_iso
from telos.memory_space.runtime import RuntimeStateStore


ROUTING_STATE_KEY = "routing"
LAST_REAL_USER_ROUTE_KEY = "last_real_user_route"
CONVERSATION_REF_KEYS = (
    "platform",
    "tenant_id",
    "app_id",
    "chat_id",
    "thread_id",
    "user_id",
    "source_id",
)


@dataclass(frozen=True)
class LastRealUserRoute:
    session_id: str
    turn_id: str
    seen_at: str
    conversation_ref: dict[str, str]

    @property
    def ref(self) -> ConversationRef:
        return conversation_ref_from_mapping(self.conversation_ref)


def record_last_real_user_route(
    state_store: RuntimeStateStore,
    *,
    ref: ConversationRef,
    session_id: str,
    turn_id: str = "",
    seen_at: str = "",
) -> LastRealUserRoute:
    route = LastRealUserRoute(
        session_id=str(session_id or "").strip(),
        turn_id=str(turn_id or "").strip(),
        seen_at=str(seen_at or user_now_iso()).strip(),
        conversation_ref=normalize_conversation_ref(asdict(ref)),
    )
    state = state_store.read()
    routing = state.get(ROUTING_STATE_KEY)
    routing = dict(routing) if isinstance(routing, dict) else {}
    routing[LAST_REAL_USER_ROUTE_KEY] = {
        "session_id": route.session_id,
        "turn_id": route.turn_id,
        "seen_at": route.seen_at,
        "conversation_ref": dict(route.conversation_ref),
    }
    state[ROUTING_STATE_KEY] = routing
    state_store.write(state)
    return route


def last_real_user_route(state_store: RuntimeStateStore) -> LastRealUserRoute | None:
    state = state_store.read()
    routing = state.get(ROUTING_STATE_KEY)
    if not isinstance(routing, dict):
        return None
    raw = routing.get(LAST_REAL_USER_ROUTE_KEY)
    if not isinstance(raw, dict):
        return None
    ref = normalize_conversation_ref(raw.get("conversation_ref"))
    if not ref.get("platform"):
        return None
    return LastRealUserRoute(
        session_id=str(raw.get("session_id") or "").strip(),
        turn_id=str(raw.get("turn_id") or "").strip(),
        seen_at=str(raw.get("seen_at") or "").strip(),
        conversation_ref=ref,
    )


def normalize_conversation_ref(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    ref = {
        key: str(value.get(key) or "")
        for key in CONVERSATION_REF_KEYS
        if value.get(key) is not None
    }
    return ref if str(ref.get("platform") or "").strip() else {}


def conversation_ref_from_mapping(raw: dict[str, Any]) -> ConversationRef:
    data = normalize_conversation_ref(raw)
    return ConversationRef(
        platform=str(data.get("platform") or ""),
        tenant_id=str(data.get("tenant_id") or ""),
        app_id=str(data.get("app_id") or ""),
        chat_id=str(data.get("chat_id") or ""),
        thread_id=str(data.get("thread_id") or ""),
        user_id=str(data.get("user_id") or ""),
        source_id=str(data.get("source_id") or ""),
    )
