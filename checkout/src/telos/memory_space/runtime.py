"""Agent runtime-only state and wire ledger."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telos.core.clock import user_now_iso
from telos.core.ids import generate_id
from telos.storage.filesystem import read_json, write_json_atomic
from telos.storage.jsonl import append_jsonl, read_jsonl


ALLOWED_RUNTIME_WIRE_EVENT_TYPES = frozenset(
    {
        "adapter.inbound.received",
        "adapter.inbound.claimed",
        "adapter.inbound.released",
        "adapter.inbound.completed",
        "adapter.cursor.committed",
        "provider.request",
        "provider.usage",
        "turn.end",
        "adapter.command",
        "adapter.delivery",
        "schedule.directMessage.sent",
    }
)


def validate_runtime_wire_event_type(event_type: str) -> str:
    clean = str(event_type or "").strip()
    if clean not in ALLOWED_RUNTIME_WIRE_EVENT_TYPES:
        raise ValueError(f"unsupported runtime wire event type: {clean or '<empty>'}")
    return clean


@dataclass(frozen=True)
class RuntimeWireEvent:
    id: str
    type: str
    timestamp: str
    session_id: str = ""
    agent_id: str = ""
    turn_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", validate_runtime_wire_event_type(self.type))

    @classmethod
    def create(
        cls,
        *,
        type: str,
        session_id: str = "",
        agent_id: str = "",
        turn_id: str = "",
        data: dict[str, Any] | None = None,
    ) -> "RuntimeWireEvent":
        return cls(
            id=generate_id("rec"),
            type=validate_runtime_wire_event_type(type),
            timestamp=user_now_iso(),
            session_id=str(session_id or ""),
            agent_id=str(agent_id or ""),
            turn_id=str(turn_id or ""),
            data=dict(data or {}),
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RuntimeWireEvent":
        return cls(
            id=str(raw["id"]),
            type=str(raw["type"]),
            timestamp=str(raw["timestamp"]),
            session_id=str(raw.get("session_id") or ""),
            agent_id=str(raw.get("agent_id") or ""),
            turn_id=str(raw.get("turn_id") or ""),
            data=dict(raw.get("data") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "turn_id": self.turn_id,
            "data": self.data,
        }


class RuntimeWireLedger:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()

    @classmethod
    def for_agent_dir(cls, root: Path) -> "RuntimeWireLedger":
        return cls(Path(root) / "runtime" / "wire.jsonl")

    def append(self, event: RuntimeWireEvent) -> None:
        append_jsonl(self.path, event.to_dict())

    def read(self) -> list[RuntimeWireEvent]:
        return [RuntimeWireEvent.from_dict(item) for item in read_jsonl(self.path)]


class RuntimeStateStore:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()

    @classmethod
    def for_agent_dir(cls, root: Path) -> "RuntimeStateStore":
        return cls(Path(root) / "runtime" / "state.json")

    def read(self) -> dict[str, Any]:
        value = read_json(self.path, {})
        return dict(value) if isinstance(value, dict) else {}

    def write(self, value: dict[str, Any]) -> None:
        write_json_atomic(self.path, dict(value))

    def update_background_cursor(self, name: str, value: Any) -> None:
        state = self.read()
        cursors = state.get("background_cursors")
        if not isinstance(cursors, dict):
            cursors = {}
        else:
            cursors = dict(cursors)
        cursors[str(name)] = value
        state["background_cursors"] = cursors
        self.write(state)

    def background_session_cursor(self, name: str, session_id: str) -> dict[str, Any]:
        state = self.read()
        cursors = state.get("background_cursors")
        if not isinstance(cursors, dict):
            return {}
        named = cursors.get(str(name))
        if not isinstance(named, dict):
            return {}
        cursor = named.get(str(session_id))
        return dict(cursor) if isinstance(cursor, dict) else {}

    def update_background_session_cursor(
        self,
        name: str,
        session_id: str,
        value: dict[str, Any],
    ) -> None:
        state = self.read()
        cursors = state.get("background_cursors")
        if not isinstance(cursors, dict):
            cursors = {}
        else:
            cursors = dict(cursors)
        named = cursors.get(str(name))
        if not isinstance(named, dict):
            named = {}
        else:
            named = dict(named)
        named[str(session_id)] = dict(value)
        cursors[str(name)] = named
        state["background_cursors"] = cursors
        self.write(state)
