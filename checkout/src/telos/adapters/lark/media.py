"""Lark media handling."""

from __future__ import annotations

from telos.adapters.base import AttachmentRef


def extract_lark_attachments(event: dict) -> list[AttachmentRef]:
    message = event.get("message") if isinstance(event.get("message"), dict) else event
    attachments: list[AttachmentRef] = []
    for item in list(message.get("attachments") or []):
        if not isinstance(item, dict):
            continue
        attachments.append(
            AttachmentRef(
                kind=str(item.get("type") or item.get("kind") or "file"),
                uri=str(item.get("file_key") or item.get("image_key") or item.get("url") or ""),
                name=str(item.get("name") or item.get("file_name") or ""),
                mime_type=str(item.get("mime_type") or ""),
            )
        )
    return attachments
