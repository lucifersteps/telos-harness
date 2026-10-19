"""Context attachment archival helpers."""

from __future__ import annotations

from pathlib import Path
from shutil import copy2
from typing import Any
from urllib.parse import unquote, urlparse

from telos.core.image_media import sha256_file, suffix_for
from telos.memory_space.paths import (
    MemorySpaceLayout,
    MemorySpaceResolver,
    is_memory_space_uri,
    memory_space_relative_path,
)
from telos.storage.filesystem import ensure_dir


def normalize_context_attachments(
    attachments: list[dict[str, Any]],
    *,
    layout: MemorySpaceLayout,
    symlink_roots: list[Path] | None = None,
) -> list[dict[str, Any]]:
    """Archive readable local attachments into context/attachments and return model-safe refs."""
    normalized: list[dict[str, Any]] = []
    roots = [_resolved_path(path) for path in list(symlink_roots or [])]
    for item in attachments:
        data = _clean_attachment(item)
        uri = data["uri"]
        if is_memory_space_uri(uri):
            if not _is_context_attachment_file(uri, layout=layout):
                data["uri"] = "error"
            normalized.append(data)
            continue
        source = _local_attachment_path(uri)
        if source is None or not source.is_file():
            data["uri"] = "error"
            normalized.append(data)
            continue
        try:
            target = _archive_context_attachment(
                source,
                layout=layout,
                fallback_name=data["name"],
                mime_type=data["mime_type"],
                use_symlink=_is_under_any_root(source, roots),
            )
        except OSError:
            data["uri"] = "error"
            normalized.append(data)
            continue
        data["uri"] = layout.uri(f"context/attachments/{target.name}")
        normalized.append(data)
    return normalized


def _clean_attachment(item: dict[str, Any]) -> dict[str, str]:
    return {
        "kind": str(item.get("kind") or ""),
        "uri": str(item.get("uri") or ""),
        "name": str(item.get("name") or ""),
        "mime_type": str(item.get("mime_type") or ""),
    }


def _is_context_attachment_file(uri: str, *, layout: MemorySpaceLayout) -> bool:
    try:
        relative = memory_space_relative_path(uri)
    except ValueError:
        return False
    if not relative.startswith("context/attachments/"):
        return False
    try:
        resolved = MemorySpaceResolver(layout).resolve(uri)
    except ValueError:
        return False
    return resolved.is_file()


def _archive_context_attachment(
    source: Path,
    *,
    layout: MemorySpaceLayout,
    fallback_name: str,
    mime_type: str,
    use_symlink: bool,
) -> Path:
    ensure_dir(layout.attachments_dir)
    suffix = suffix_for(source=source, fallback_name=fallback_name, mime_type=mime_type)
    digest = sha256_file(source)
    target = layout.attachments_dir / f"att_{digest}{suffix}"
    if target.exists():
        return target
    if target.is_symlink():
        target.unlink()
    if use_symlink:
        target.symlink_to(_resolved_path(source))
        return target
    copy2(source, target)
    return target


def _is_under_any_root(path: Path, roots: list[Path]) -> bool:
    if not roots:
        return False
    resolved = _resolved_path(path)
    return any(_is_relative_to(resolved, root) for root in roots)


def _resolved_path(path: Path) -> Path:
    return Path(path).expanduser().resolve()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _local_attachment_path(uri: str) -> Path | None:
    value = str(uri or "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme and parsed.scheme != "file":
        return None
    if parsed.scheme == "file":
        if parsed.netloc and parsed.netloc not in {"localhost", "127.0.0.1"}:
            return None
        return Path(unquote(parsed.path)).expanduser()
    return Path(value).expanduser()
