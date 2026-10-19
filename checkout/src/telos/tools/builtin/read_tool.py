"""Unified local Read tool for text and media files."""

from __future__ import annotations

import mimetypes
import struct
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from telos.agent.tools.types import ExecutableTool, ToolResult, create_executable_tool
from telos.memory_space.paths import (
    AgentRuntimeLayout,
    MemorySpaceLayout,
    MemorySpaceResolver,
    is_memory_space_uri,
    memory_space_relative_path,
)
from telos.tools.builtin._paths import (
    configured_allowed_roots,
    configured_deny_patterns,
    is_denied_by_patterns,
)

MAX_TEXT_LINES = 1000
MAX_LINE_LENGTH = 2000
MAX_TEXT_OUTPUT_BYTES = 100 * 1024
MAX_MEDIA_BYTES = 100 * 1024 * 1024
MEDIA_SNIFF_BYTES = 512

BLOCKED_PARTS = {".git", ".venv", "venv", "__pycache__", "sessions", "logs"}

SENSITIVE_BASENAMES = {
    ".env",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "credentials",
}
SENSITIVE_ENV_EXEMPTIONS = {".env.example", ".env.sample", ".env.template"}
SENSITIVE_PUBLIC_KEY_EXEMPTIONS = {"id_rsa.pub", "id_ed25519.pub", "id_ecdsa.pub"}
SENSITIVE_PREFIXES = ("id_rsa", "id_ed25519", "id_ecdsa", "credentials")
SENSITIVE_DOT_SUFFIXES = {
    ".bak",
    ".backup",
    ".copy",
    ".disabled",
    ".key",
    ".old",
    ".orig",
    ".pem",
    ".save",
    ".tmp",
}

IMAGE_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".avif": "image/avif",
}
VIDEO_MIME_BY_SUFFIX = {
    ".mp4": "video/mp4",
    ".mpg": "video/mpeg",
    ".mpeg": "video/mpeg",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".ogv": "video/ogg",
    ".wmv": "video/x-ms-wmv",
    ".webm": "video/webm",
    ".m4v": "video/x-m4v",
    ".flv": "video/x-flv",
}
NON_TEXT_SUFFIXES = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".pages",
    ".numbers",
    ".key",
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".zst",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".mp3",
    ".wav",
    ".flac",
    ".ogg",
    ".aac",
    ".m4a",
    ".ttf",
    ".otf",
    ".woff",
    ".woff2",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".bin",
    ".dmg",
    ".iso",
    ".wasm",
}


@dataclass(frozen=True)
class ReadAccessPolicy:
    allowed_roots: list[Path]
    deny_patterns: tuple[str, ...] = ()
    default_denies: bool = True
    memory_space_root: Path | None = None
    runtime_timeline_screenshots_root: Path | None = None


def create_read_tool(
    *,
    repo_root: Path | None = None,
    data_dir: Path | None = None,
    memory_space_dir: Path | None = None,
    options: dict[str, Any] | None = None,
) -> ExecutableTool:
    policy = _read_access_policy(
        repo_root=repo_root,
        data_dir=data_dir,
        memory_space_dir=memory_space_dir,
        options=options or {},
    )
    return create_executable_tool(
        name="Read",
        description=(
            "从配置允许的本地根目录读取 UTF-8 文本、图片或视频文件。读取文本文件时可用 "
            "line_offset 和 n_lines 分页。相对路径会基于第一个允许根目录解析；绝对路径和 "
            "file:// URI 必须位于允许根目录内；memory_space:// URI 可读取 memory、context、"
            "recaps、timeline 和 knowledge 下的文件。被拒绝的路径和敏感文件会被拒读。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "本地文件路径、file:// URI 或 memory_space:// URI。",
                },
                "line_offset": {
                    "type": "integer",
                    "minimum": -MAX_TEXT_LINES,
                    "description": (
                        "从哪一行开始读取文本，按 1 起算。负数表示从文件末尾倒数读取；"
                        f"最小值为 -{MAX_TEXT_LINES}。省略时从第 1 行开始。"
                    ),
                },
                "n_lines": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_TEXT_LINES,
                    "description": (
                        f"要读取的文本行数。省略时最多读取 {MAX_TEXT_LINES} 行。"
                    ),
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=lambda args: read_path(
            policy,
            str(args["path"]),
            line_offset=_optional_int(args.get("line_offset"), default=1),
            n_lines=_optional_int(args.get("n_lines"), default=MAX_TEXT_LINES),
        ),
    )


def read_path(
    policy: ReadAccessPolicy,
    path: str,
    *,
    line_offset: int = 1,
    n_lines: int = MAX_TEXT_LINES,
) -> object:
    if line_offset == 0:
        return ToolResult(output="line_offset cannot be 0", is_error=True)
    if line_offset < -MAX_TEXT_LINES:
        return ToolResult(
            output=f"line_offset cannot be less than -{MAX_TEXT_LINES}",
            is_error=True,
        )
    if n_lines <= 0:
        return ToolResult(output="n_lines must be positive", is_error=True)

    file_path = _resolve_read_path(policy, path)
    if not file_path.exists():
        return ToolResult(output=f'"{path}" does not exist.', is_error=True)
    if not file_path.is_file():
        return ToolResult(output=f'"{path}" is not a file.', is_error=True)

    stat = file_path.stat()
    header = _read_prefix(file_path, MEDIA_SNIFF_BYTES)
    file_type = _detect_file_type(file_path, header)
    if file_type["kind"] == "text":
        return _read_text_file(
            file_path,
            display_path=path,
            line_offset=line_offset,
            n_lines=min(n_lines, MAX_TEXT_LINES),
            requested_lines=n_lines,
        )
    if file_type["kind"] in {"image", "video"}:
        return _read_media_file(
            file_path,
            display_path=path,
            file_type=file_type,
            size=stat.st_size,
        )
    return ToolResult(
        output=(
            f'"{path}" is not readable as UTF-8 text, image, or video. '
            "Use a document-specific tool, Bash, or an MCP tool for this file type."
        ),
        is_error=True,
    )


def _read_text_file(
    file_path: Path,
    *,
    display_path: str,
    line_offset: int,
    n_lines: int,
    requested_lines: int,
) -> ToolResult:
    try:
        if line_offset < 0:
            return _read_text_tail(
                file_path,
                display_path=display_path,
                line_offset=line_offset,
                n_lines=n_lines,
                requested_lines=requested_lines,
            )
        return _read_text_forward(
            file_path,
            display_path=display_path,
            line_offset=line_offset,
            n_lines=n_lines,
            requested_lines=requested_lines,
        )
    except UnicodeDecodeError:
        return ToolResult(output=_not_text_output(display_path), is_error=True)


def _read_text_forward(
    file_path: Path,
    *,
    display_path: str,
    line_offset: int,
    n_lines: int,
    requested_lines: int,
) -> ToolResult:
    selected: list[tuple[int, str]] = []
    total_lines = 0
    line_endings = _LineEndingFlags()
    nul_found = False
    with file_path.open("r", encoding="utf-8", errors="strict", newline="") as handle:
        for raw_line in handle:
            if "\x00" in raw_line:
                nul_found = True
                break
            total_lines += 1
            line_endings.update(raw_line)
            if total_lines < line_offset:
                continue
            if len(selected) < n_lines:
                selected.append((total_lines, _strip_trailing_lf(raw_line)))

    if nul_found:
        return ToolResult(output=_not_text_output(display_path), is_error=True)
    max_lines_reached = total_lines >= line_offset + n_lines and n_lines >= MAX_TEXT_LINES
    return _finish_text_output(
        selected,
        total_lines=total_lines,
        start_line=line_offset if selected else 0,
        requested_lines=requested_lines,
        max_lines_reached=max_lines_reached,
        line_endings=line_endings,
    )


def _read_text_tail(
    file_path: Path,
    *,
    display_path: str,
    line_offset: int,
    n_lines: int,
    requested_lines: int,
) -> ToolResult:
    tail_count = abs(line_offset)
    entries: deque[tuple[int, str]] = deque(maxlen=tail_count)
    total_lines = 0
    line_endings = _LineEndingFlags()
    nul_found = False
    with file_path.open("r", encoding="utf-8", errors="strict", newline="") as handle:
        for raw_line in handle:
            if "\x00" in raw_line:
                nul_found = True
                break
            total_lines += 1
            line_endings.update(raw_line)
            entries.append((total_lines, _strip_trailing_lf(raw_line)))

    if nul_found:
        return ToolResult(output=_not_text_output(display_path), is_error=True)

    selected = list(entries)[:n_lines]
    return _finish_text_output(
        selected,
        total_lines=total_lines,
        start_line=selected[0][0] if selected else 0,
        requested_lines=requested_lines,
        max_lines_reached=False,
        line_endings=line_endings,
        keep_tail_on_byte_limit=True,
    )


def _finish_text_output(
    selected: list[tuple[int, str]],
    *,
    total_lines: int,
    start_line: int,
    requested_lines: int,
    max_lines_reached: bool,
    line_endings: "_LineEndingFlags",
    keep_tail_on_byte_limit: bool = False,
) -> ToolResult:
    style = line_endings.style()
    rendered: list[str] = []
    truncated_line_numbers: list[int] = []
    max_bytes_reached = False

    candidates: list[tuple[int, str]] = []
    for line_no, raw_content in selected:
        model_content = raw_content[:-1] if style == "crlf" and raw_content.endswith("\r") else raw_content
        rendered_content = _truncate_line(model_content)
        if style == "mixed":
            rendered_content = rendered_content.replace("\r", "\\r")
        if rendered_content != model_content:
            truncated_line_numbers.append(line_no)
        candidates.append((line_no, f"{line_no}\t{rendered_content}"))

    if keep_tail_on_byte_limit:
        total_bytes = sum(_line_bytes(line, index == 0) for index, (_line_no, line) in enumerate(candidates))
        if total_bytes > MAX_TEXT_OUTPUT_BYTES:
            max_bytes_reached = True
            kept: list[str] = []
            byte_count = 0
            for _line_no, line in reversed(candidates):
                line_bytes = _line_bytes(line, not kept)
                if byte_count + line_bytes > MAX_TEXT_OUTPUT_BYTES:
                    break
                kept.insert(0, line)
                byte_count += line_bytes
            rendered = kept
        else:
            rendered = [line for _line_no, line in candidates]
    else:
        byte_count = 0
        for index, (_line_no, line) in enumerate(candidates):
            line_bytes = _line_bytes(line, index == 0)
            if rendered and byte_count + line_bytes > MAX_TEXT_OUTPUT_BYTES:
                max_bytes_reached = True
                break
            rendered.append(line)
            byte_count += line_bytes
            if byte_count >= MAX_TEXT_OUTPUT_BYTES:
                max_bytes_reached = True
                break

    status = _text_status(
        line_count=len(rendered),
        total_lines=total_lines,
        start_line=start_line if rendered else 0,
        requested_lines=requested_lines,
        max_lines_reached=max_lines_reached,
        max_bytes_reached=max_bytes_reached,
        truncated_line_numbers=truncated_line_numbers,
        line_ending_style=style,
    )
    body = "\n".join(rendered)
    output = f"{body}\n<system>{status}</system>" if body else f"<system>{status}</system>"
    return ToolResult(output=output)


def _read_media_file(
    file_path: Path,
    *,
    display_path: str,
    file_type: dict[str, str],
    size: int,
) -> ToolResult:
    if size <= 0:
        return ToolResult(output=f'"{display_path}" is empty.', is_error=True)
    if size > MAX_MEDIA_BYTES:
        return ToolResult(
            output=(
                f'"{display_path}" is {size} bytes, which exceeds the maximum '
                f"{MAX_MEDIA_BYTES // 1024 // 1024}MB for media files."
            ),
            is_error=True,
        )

    kind = file_type["kind"]
    mime_type = file_type["mime_type"]
    image = _image_info(_read_prefix(file_path, 131_072)) if kind == "image" else None
    system = _media_system_summary(
        kind=kind,
        mime_type=mime_type,
        byte_size=size,
        image=image,
    )
    part_type = "image_file" if kind == "image" else "video_file"
    tag = "image" if kind == "image" else "video"
    content_path = display_path if is_memory_space_uri(display_path) else str(file_path)
    return ToolResult(
        output=[
            {"type": "text", "text": system},
            {"type": "text", "text": f'<{tag} path="{content_path}">'},
            {
                "type": part_type,
                "path": content_path,
                "mime_type": mime_type,
                "size_bytes": size,
            },
            {"type": "text", "text": f"</{tag}>"},
        ]
    )


def _media_system_summary(
    *,
    kind: str,
    mime_type: str,
    byte_size: int,
    image: dict[str, Any] | None,
) -> str:
    parts = [
        f"Read {kind} file.",
        f"Mime type: {mime_type}.",
        f"Size: {byte_size} bytes.",
    ]
    if kind == "image" and image is not None:
        parts.append(
            f"Original dimensions: {image['width']}x{image['height']} pixels."
        )
        parts.append(
            "If coordinates are needed, output relative coordinates first and compute "
            "absolute coordinates using the original image size."
        )
    parts.append(
        "If you generate or edit images or videos via commands or scripts, read the "
        "result back immediately before continuing."
    )
    return f"<system>{' '.join(parts)}</system>"


class _LineEndingFlags:
    def __init__(self) -> None:
        self.has_crlf = False
        self.has_lf = False
        self.has_lone_cr = False

    def update(self, text: str) -> None:
        index = 0
        while index < len(text):
            char = text[index]
            if char == "\r":
                if index + 1 < len(text) and text[index + 1] == "\n":
                    self.has_crlf = True
                    index += 2
                    continue
                self.has_lone_cr = True
            elif char == "\n":
                self.has_lf = True
            index += 1

    def style(self) -> str:
        if self.has_lone_cr or (self.has_crlf and self.has_lf):
            return "mixed"
        if self.has_crlf:
            return "crlf"
        return "lf"


def _text_status(
    *,
    line_count: int,
    total_lines: int,
    start_line: int,
    requested_lines: int,
    max_lines_reached: bool,
    max_bytes_reached: bool,
    truncated_line_numbers: list[int],
    line_ending_style: str,
) -> str:
    if line_count:
        word = "line" if line_count == 1 else "lines"
        parts = [f"{line_count} {word} read from file starting from line {start_line}."]
    else:
        parts = ["No lines read from file."]
    parts.append(f"Total lines in file: {total_lines}.")
    if max_lines_reached:
        parts.append(f"Max {MAX_TEXT_LINES} lines reached.")
    elif max_bytes_reached:
        parts.append(f"Max {MAX_TEXT_OUTPUT_BYTES} bytes reached.")
    elif line_count < requested_lines:
        parts.append("End of file reached.")
    if truncated_line_numbers:
        parts.append(f"Lines [{', '.join(str(item) for item in truncated_line_numbers)}] were truncated.")
    if line_ending_style == "mixed":
        parts.append(
            "Mixed or lone carriage-return line endings are shown as \\r."
        )
    return " ".join(parts)


def _resolve_read_path(policy: ReadAccessPolicy, path: str) -> Path:
    raw_path = str(path or "").strip()
    if not raw_path:
        raise ValueError("Path is required")
    if is_memory_space_uri(raw_path):
        return _resolve_memory_space_read_path(policy, raw_path)

    parsed = urlparse(raw_path)
    if parsed.scheme in {"http", "https"}:
        raise ValueError("Read only accepts local file paths or file:// URIs")
    if parsed.scheme and parsed.scheme != "file":
        raise ValueError(f"Read does not support URI scheme: {parsed.scheme}")
    if parsed.scheme == "file":
        if parsed.netloc and parsed.netloc not in {"localhost", "127.0.0.1"}:
            raise ValueError(f"file:// host is not local: {parsed.netloc}")
        candidate = Path(unquote(parsed.path))
    else:
        candidate = Path(raw_path).expanduser()

    if not candidate.is_absolute():
        parts = candidate.parts
        first_root = policy.allowed_roots[0]
        if parts and parts[0] == first_root.name:
            candidate = Path(*parts[1:]) if len(parts) > 1 else Path(".")
        candidate = first_root / candidate
    resolved = candidate.resolve()
    for root in policy.allowed_roots:
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            continue
        _ensure_path_allowed(relative, policy)
        return resolved
    raise ValueError("Path must stay inside an allowed Read root")


def _resolve_memory_space_read_path(policy: ReadAccessPolicy, uri: str) -> Path:
    if policy.memory_space_root is None:
        raise ValueError("Read requires memory_space_dir for memory_space:// URIs")
    relative = memory_space_relative_path(uri)
    if not _is_readable_memory_space_path(relative):
        raise ValueError(
            "Read only supports memory_space://memory, context, recaps, timeline, "
            "or knowledge paths"
        )
    layout = MemorySpaceLayout.for_root(
        policy.memory_space_root,
        runtime_timeline_screenshots_dir=policy.runtime_timeline_screenshots_root,
    )
    return MemorySpaceResolver(layout).resolve(uri)


def _is_readable_memory_space_path(relative: str) -> bool:
    return any(
        relative == prefix or relative.startswith(f"{prefix}/")
        for prefix in ("memory", "context", "recaps", "timeline", "knowledge")
    )


def _ensure_path_allowed(relative_path: Path, policy: ReadAccessPolicy) -> None:
    if is_denied_by_patterns(relative_path, policy.deny_patterns):
        raise ValueError(f"Cannot read denied file: {relative_path}")
    if not policy.default_denies:
        return
    if any(part in BLOCKED_PARTS for part in relative_path.parts):
        raise ValueError(f"Cannot read blocked path: {relative_path}")
    if _is_sensitive_file(relative_path):
        raise ValueError(f"Cannot read sensitive file: {relative_path}")


def _is_sensitive_file(path: Path) -> bool:
    name = path.name.lower()
    path_text = "/".join(part.lower() for part in path.parts)
    if name in SENSITIVE_ENV_EXEMPTIONS or name in SENSITIVE_PUBLIC_KEY_EXEMPTIONS:
        return False
    if name in SENSITIVE_BASENAMES or name.startswith(".env."):
        return True
    for prefix in SENSITIVE_PREFIXES:
        if name == prefix:
            return True
        if name.startswith(prefix):
            suffix = name[len(prefix) :]
            if suffix.startswith(("-", "_")):
                return True
            if suffix.startswith(".") and suffix in SENSITIVE_DOT_SUFFIXES:
                return True
    return (
        path_text.endswith(".aws/credentials")
        or "/.aws/credentials/" in path_text
        or path_text.endswith(".gcp/credentials")
        or "/.gcp/credentials/" in path_text
    )


def _allowed_roots(*, repo_root: Path | None, data_dir: Path | None) -> list[Path]:
    roots: list[Path] = []
    for root in (repo_root, data_dir):
        if root is None:
            continue
        resolved = root.expanduser().resolve()
        if resolved not in roots:
            roots.append(resolved)
    if not roots:
        raise ValueError("Read requires repo_root or data_dir")
    return roots


def _read_access_policy(
    *,
    repo_root: Path | None,
    data_dir: Path | None,
    memory_space_dir: Path | None,
    options: dict[str, Any],
) -> ReadAccessPolicy:
    roots = configured_allowed_roots(
        options,
        repo_root=repo_root,
        data_dir=data_dir,
        option_label="Read allowed path",
    ) or _allowed_roots(repo_root=repo_root, data_dir=data_dir)
    return ReadAccessPolicy(
        allowed_roots=roots,
        deny_patterns=tuple(configured_deny_patterns(options)),
        default_denies=_option_bool(options, "default_denies", default=True),
        memory_space_root=memory_space_dir.expanduser().resolve()
        if memory_space_dir is not None
        else None,
        runtime_timeline_screenshots_root=_runtime_timeline_screenshots_root(data_dir)
        if memory_space_dir is not None
        else None,
    )


def _runtime_timeline_screenshots_root(data_dir: Path | None) -> Path | None:
    if data_dir is None:
        return None
    return AgentRuntimeLayout.for_agent_dir(data_dir).runtime_timeline_screenshots_dir


def _option_bool(options: dict[str, Any], key: str, *, default: bool) -> bool:
    value = options.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _detect_file_type(path: Path, header: bytes) -> dict[str, str]:
    sniffed = _sniff_media(path, header)
    if sniffed is not None:
        return sniffed
    if b"\x00" in header:
        return {"kind": "unknown", "mime_type": ""}

    suffix = path.suffix.lower()
    if suffix == ".svg":
        return {"kind": "text", "mime_type": "image/svg+xml"}
    if suffix in IMAGE_MIME_BY_SUFFIX:
        return {"kind": "image", "mime_type": IMAGE_MIME_BY_SUFFIX[suffix]}
    if suffix in VIDEO_MIME_BY_SUFFIX:
        return {"kind": "video", "mime_type": VIDEO_MIME_BY_SUFFIX[suffix]}
    if suffix in NON_TEXT_SUFFIXES:
        return {"kind": "unknown", "mime_type": ""}

    guessed, _encoding = mimetypes.guess_type(path.name)
    if guessed:
        if guessed.startswith("text/"):
            return {"kind": "text", "mime_type": guessed}
        if guessed.startswith("image/"):
            return {"kind": "image", "mime_type": guessed}
        if guessed.startswith("video/"):
            return {"kind": "video", "mime_type": guessed}
    return {"kind": "text", "mime_type": "text/plain"}


def _sniff_media(path: Path, header: bytes) -> dict[str, str] | None:
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return {"kind": "image", "mime_type": "image/png"}
    if header.startswith(b"\xff\xd8\xff"):
        return {"kind": "image", "mime_type": "image/jpeg"}
    if header[:6] in {b"GIF87a", b"GIF89a"}:
        return {"kind": "image", "mime_type": "image/gif"}
    if header.startswith(b"BM"):
        return {"kind": "image", "mime_type": "image/bmp"}
    if header.startswith(b"RIFF") and len(header) >= 12:
        chunk = header[8:12]
        if chunk == b"WEBP":
            return {"kind": "image", "mime_type": "image/webp"}
        if chunk == b"AVI ":
            return {"kind": "video", "mime_type": "video/x-msvideo"}
    if header.startswith(b"FLV"):
        return {"kind": "video", "mime_type": "video/x-flv"}
    brand = _ftyp_brand(header)
    if brand in {"isom", "iso2", "iso5", "mp41", "mp42", "avc1", "mp4v", "m4v"}:
        return {"kind": "video", "mime_type": VIDEO_MIME_BY_SUFFIX.get(path.suffix.lower(), "video/mp4")}
    if brand in {"qt"}:
        return {"kind": "video", "mime_type": "video/quicktime"}
    if brand in {"avif", "avis"}:
        return {"kind": "image", "mime_type": "image/avif"}
    if brand in {"heic", "heif", "heix", "hevc", "mif1", "msf1"}:
        return {"kind": "image", "mime_type": IMAGE_MIME_BY_SUFFIX.get(path.suffix.lower(), "image/heif")}
    return None


def _ftyp_brand(header: bytes) -> str:
    if len(header) < 12 or header[4:8] != b"ftyp":
        return ""
    return header[8:12].decode("latin1", errors="ignore").rstrip("\x00 ").strip().lower()


def _image_info(header: bytes) -> dict[str, Any] | None:
    png = _png_size(header)
    if png is not None:
        width, height = png
        return {"format": "png", "width": width, "height": height}
    jpeg = _jpeg_size(header)
    if jpeg is not None:
        width, height = jpeg
        return {"format": "jpeg", "width": width, "height": height}
    gif = _gif_size(header)
    if gif is not None:
        width, height = gif
        return {"format": "gif", "width": width, "height": height}
    webp = _webp_size(header)
    if webp is not None:
        width, height = webp
        return {"format": "webp", "width": width, "height": height}
    return None


def _png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def _gif_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 10 or data[:6] not in {b"GIF87a", b"GIF89a"}:
        return None
    return struct.unpack("<HH", data[6:10])


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    index = 2
    sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            return None
        marker = data[index]
        index += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if index + 2 > len(data):
            return None
        segment_length = struct.unpack(">H", data[index : index + 2])[0]
        if segment_length < 2 or index + segment_length > len(data):
            return None
        if marker in sof_markers and segment_length >= 7:
            height = struct.unpack(">H", data[index + 3 : index + 5])[0]
            width = struct.unpack(">H", data[index + 5 : index + 7])[0]
            return width, height
        index += segment_length
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if chunk == b"VP8 " and len(data) >= 26:
        width = struct.unpack("<H", data[20:22])[0] & 0x3FFF
        height = struct.unpack("<H", data[22:24])[0] & 0x3FFF
        return width, height
    if chunk == b"VP8L" and len(data) >= 25:
        b1, b2, b3, b4 = data[21:25]
        width = 1 + (((b2 & 0x3F) << 8) | b1)
        height = 1 + (((b4 & 0x0F) << 10) | (b3 << 2) | ((b2 & 0xC0) >> 6))
        return width, height
    return None


def _read_prefix(path: Path, limit: int) -> bytes:
    with path.open("rb") as handle:
        return handle.read(limit)


def _strip_trailing_lf(line: str) -> str:
    return line[:-1] if line.endswith("\n") else line


def _truncate_line(line: str) -> str:
    if len(line) <= MAX_LINE_LENGTH:
        return line
    return line[: MAX_LINE_LENGTH - 3].rstrip() + "..."


def _line_bytes(line: str, is_first: bool) -> int:
    return len(line.encode("utf-8")) + (0 if is_first else 1)


def _not_text_output(path: str) -> str:
    return (
        f'"{path}" is not readable as UTF-8 text. '
        "If it is an image or video, Read will route it as media; otherwise use a "
        "document-specific tool, Bash, or an MCP tool."
    )


def _optional_int(value: object, *, default: int) -> int:
    if value is None:
        return default
    return int(value)
