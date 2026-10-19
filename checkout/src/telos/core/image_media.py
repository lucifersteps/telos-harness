"""Shared content-addressing + image-type helpers.

Both context attachment archival (``memory_space/attachments.py``) and WeChat
media caching (``adapters/wechat/media.py``) content-address blobs by sha256 and
need to decide a file suffix / mime for an image. This module is the single home
for that logic so the two callers stay in sync.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_GIF_MAGICS = (b"GIF87a", b"GIF89a")

# mime -> canonical suffix (superset of both former call sites).
_MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
}

# suffix -> mime for images (default jpeg).
_EXT_TO_MIME = {
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def looks_like_image(data: bytes) -> bool:
    if len(data) < 4:
        return False
    if data[:8] == _PNG_MAGIC:
        return True
    if data[:3] == _JPEG_MAGIC:
        return True
    if data[:6] in _GIF_MAGICS:
        return True
    return bool(data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP")


def image_ext_from_bytes(data: bytes) -> str:
    """Magic-byte sniff of an image's suffix; defaults to ``.jpg``."""
    if data[:8] == _PNG_MAGIC:
        return ".png"
    if data[:6] in _GIF_MAGICS:
        return ".gif"
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def mime_for_ext(suffix: str) -> str:
    """Image mime for a file suffix; defaults to ``image/jpeg``."""
    return _EXT_TO_MIME.get(str(suffix or "").lower(), "image/jpeg")


def ext_for_mime(mime_type: str) -> str:
    """Canonical suffix for a mime type; empty string when unknown."""
    return _MIME_TO_EXT.get(str(mime_type or "").strip().lower(), "")


def suffix_for(
    *,
    source: Path | None = None,
    data: bytes | None = None,
    fallback_name: str = "",
    mime_type: str = "",
) -> str:
    """Pick a file suffix with a unified precedence.

    magic-byte sniff (when ``data`` given) -> source suffix -> fallback name
    suffix -> mime->ext map. Empty string when nothing resolves.
    """
    if data is not None and looks_like_image(data):
        return image_ext_from_bytes(data)
    if source is not None:
        suffix = Path(source).suffix
        if suffix:
            return suffix
    fallback_suffix = Path(str(fallback_name or "")).suffix
    if fallback_suffix:
        return fallback_suffix
    return ext_for_mime(mime_type)
