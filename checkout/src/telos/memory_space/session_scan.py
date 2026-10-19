"""会话内存扫描。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telos.memory_space.context import ContextMessageRepository
from telos.memory_space.paths import (
    agent_dir_for_session_dir,
    memory_space_root_for_session,
)
from telos.memory_space.runtime import RuntimeWireLedger
from telos.session.session import Session

_ATTACHMENT_FIELD_RE = re.compile(r"(kind|mime_type|path)=([^,\n]+)")


@dataclass(frozen=True)
class MemorySpaceSessionScan:
    first_activity_at: str = ""
    last_activity_at: str = ""
    last_user_message_at: str = ""
    first_user_input: str = ""
    context_message_count: int = 0
    runtime_event_count: int = 0
    attachment_count: int = 0
    attachment_refs: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "first_activity_at": self.first_activity_at,
            "last_activity_at": self.last_activity_at,
            "last_user_message_at": self.last_user_message_at,
            "first_user_input": self.first_user_input,
            "context_message_count": self.context_message_count,
            "runtime_event_count": self.runtime_event_count,
            "attachment_count": self.attachment_count,
            "attachment_refs": list(self.attachment_refs),
        }


def scan_session_memory(session: Session) -> MemorySpaceSessionScan:
    session_dir = Path(session.directory)
    agent_dir = agent_dir_for_session_dir(session_dir)
    memory_space_root = memory_space_root_for_session(session)
    with ContextMessageRepository.for_memory_space(memory_space_root) as context_repo:
        context_messages = context_repo.list_messages(session_id=session.id)
    runtime_events = [
        event
        for event in RuntimeWireLedger.for_agent_dir(agent_dir).read()
        if event.session_id == session.id
    ]

    activity_times = [
        *(message.created_at for message in context_messages if message.created_at),
        *(event.timestamp for event in runtime_events if event.timestamp),
    ]
    user_messages = [message for message in context_messages if message.role == "user"]
    attachment_refs = _attachment_refs_from_context_messages(context_messages)

    first_user_input = ""
    if user_messages:
        first_user_input = _strip_attachment_marker(
            _content_text(user_messages[0].content)
        )

    return MemorySpaceSessionScan(
        first_activity_at=min(activity_times) if activity_times else "",
        last_activity_at=max(activity_times) if activity_times else "",
        last_user_message_at=user_messages[-1].created_at if user_messages else "",
        first_user_input=first_user_input,
        context_message_count=len(context_messages),
        runtime_event_count=len(runtime_events),
        attachment_count=len(attachment_refs),
        attachment_refs=attachment_refs,
    )


def _attachment_refs_from_context_messages(messages: list[Any]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    seen_uris: set[str] = set()
    for message in messages:
        _collect_attachment_refs_from_content(
            getattr(message, "content", None),
            refs=refs,
            seen_uris=seen_uris,
        )
    return refs


def _collect_attachment_refs_from_content(
    content: object,
    *,
    refs: list[dict[str, str]],
    seen_uris: set[str],
) -> None:
    if isinstance(content, str):
        for item in _attachment_refs_from_text(content):
            _append_attachment_ref(item, refs=refs, seen_uris=seen_uris)
        return
    if isinstance(content, list):
        for item in content:
            _collect_attachment_refs_from_content(
                item,
                refs=refs,
                seen_uris=seen_uris,
            )
        return
    if not isinstance(content, dict):
        return
    part_type = str(content.get("type") or "")
    if part_type == "text":
        _collect_attachment_refs_from_content(
            str(content.get("text") or ""),
            refs=refs,
            seen_uris=seen_uris,
        )
        return
    uri = str(content.get("path") or "").strip()
    if not uri:
        return
    if part_type == "image_file":
        kind = "image"
    elif part_type == "video_file":
        kind = "video"
    else:
        kind = "file"
    _append_attachment_ref(
        {
            "kind": kind,
            "uri": uri,
            "name": _attachment_name_from_uri(uri),
            "mime_type": str(content.get("mime_type") or ""),
        },
        refs=refs,
        seen_uris=seen_uris,
    )


def _attachment_refs_from_text(text: str) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or "path=" not in line or "kind=" not in line:
            continue
        line = re.sub(r"^\d+\.\s*", "", line)
        fields = {
            match.group(1): match.group(2).strip()
            for match in _ATTACHMENT_FIELD_RE.finditer(line)
        }
        uri = fields.get("path", "")
        if not uri:
            continue
        refs.append(
            {
                "kind": fields.get("kind", ""),
                "uri": uri,
                "name": _attachment_name_from_uri(uri),
                "mime_type": fields.get("mime_type", ""),
            }
        )
    return refs


def _append_attachment_ref(
    item: dict[str, str],
    *,
    refs: list[dict[str, str]],
    seen_uris: set[str],
) -> None:
    uri = str(item.get("uri") or "").strip()
    if uri and uri != "error":
        if uri in seen_uris:
            return
        seen_uris.add(uri)
    refs.append(
        {
            "kind": str(item.get("kind") or ""),
            "uri": uri,
            "name": str(item.get("name") or ""),
            "mime_type": str(item.get("mime_type") or ""),
        }
    )


def _attachment_name_from_uri(uri: str) -> str:
    value = str(uri or "").strip()
    if not value or value == "error":
        return ""
    return Path(value.removeprefix("memory_space://")).name


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part).strip()
    return str(content or "").strip()


def _strip_attachment_marker(text: str) -> str:
    value = str(text or "").strip()
    marker = "\n\n[received "
    if marker in value:
        return value.split(marker, 1)[0].strip()
    if value.startswith("[received "):
        return ""
    return value
