"""Telemetry interfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from telos.core.clock import user_now_iso


@dataclass(frozen=True)
class TelemetryEvent:
    name: str
    fields: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=user_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "timestamp": self.timestamp,
            "fields": dict(self.fields),
        }


class TelemetrySink(Protocol):
    def emit(self, event: TelemetryEvent) -> None: ...


class NullTelemetrySink:
    def emit(self, event: TelemetryEvent) -> None:
        del event


@dataclass
class InMemoryTelemetrySink:
    events: list[TelemetryEvent] = field(default_factory=list)

    def emit(self, event: TelemetryEvent) -> None:
        self.events.append(event)

    def to_dicts(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self.events]


def emit_telemetry(
    sink: TelemetrySink | None,
    name: str,
    **fields: Any,
) -> TelemetryEvent:
    event = TelemetryEvent(name=name, fields=dict(fields))
    (sink or NullTelemetrySink()).emit(event)
    return event
