"""Config-scoped Write and Edit tools for local text files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from telos.agent.tools.types import ExecutableTool, ToolResult, create_executable_tool
from telos.memory_space.memory_files import validate_memory_file_content
from telos.memory_space.paths import (
    MemorySpaceLayout,
    is_memory_space_uri,
    memory_space_relative_path,
)
from telos.tools.builtin._paths import (
    configured_allowed_roots,
    configured_deny_patterns,
    is_denied_by_patterns,
)

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
APPROVAL_PREVIEW_CHARS = 800


@dataclass(frozen=True)
class FileMutationAccessPolicy:
    allowed_roots: list[Path]
    deny_patterns: tuple[str, ...] = ()
    default_denies: bool = True
    memory_space_root: Path | None = None


def create_write_tool(
    *,
    repo_root: Path | None = None,
    data_dir: Path | None = None,
    memory_space_dir: Path | None = None,
    options: dict[str, Any] | None = None,
) -> ExecutableTool:
    policy = _file_mutation_access_policy(
        repo_root=repo_root,
        data_dir=data_dir,
        memory_space_dir=memory_space_dir,
        options=options or {},
    )
    return create_executable_tool(
        name="Write",
        description=(
            "创建、完全覆盖或追加写入配置允许的本地 UTF-8 文本文件。小范围精确修改请使用 "
            "Edit。memory_space:// URI 只允许写入 memory/ 下的文件。父目录必须已经存在；"
            "追加写入会按提供内容原样写入，不会额外添加换行。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "要创建、追加或完全覆盖的文件路径，可使用 memory_space://memory/...。"
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "要原样写入的完整文件内容。",
                },
                "mode": {
                    "type": "string",
                    "enum": ["overwrite", "append"],
                    "description": "写入模式。默认 overwrite。",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        handler=lambda args: write_path(
            policy,
            path=str(args["path"]),
            content=str(args.get("content") or ""),
            mode=str(args.get("mode") or "overwrite"),
        ),
        need_confirm=True,
        approval_preview=lambda args: _write_approval_preview(policy, args),
    )


def create_edit_tool(
    *,
    repo_root: Path | None = None,
    data_dir: Path | None = None,
    memory_space_dir: Path | None = None,
    options: dict[str, Any] | None = None,
) -> ExecutableTool:
    policy = _file_mutation_access_policy(
        repo_root=repo_root,
        data_dir=data_dir,
        memory_space_dir=memory_space_dir,
        options=options or {},
    )
    return create_executable_tool(
        name="Edit",
        description=(
            "在配置允许的本地 UTF-8 文本文件中替换精确字符串。默认要求 old_string 只匹配"
            "一次；只有需要替换所有匹配项时才设置 replace_all=true。memory_space:// URI "
            "只允许编辑 memory/ 下的文件。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "要编辑的文本文件路径，可使用 memory_space://memory/...。",
                },
                "old_string": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "要替换的精确内容，来自 Read 输出视图，但不包含行号前缀。"
                    ),
                },
                "new_string": {
                    "type": "string",
                    "description": "替换文本，应与 Read 输出视图中的文本一致。",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": (
                        "仅当需要替换 old_string 的所有出现位置时设为 true。"
                    ),
                },
            },
            "required": ["path", "old_string", "new_string"],
            "additionalProperties": False,
        },
        handler=lambda args: edit_path(
            policy,
            path=str(args["path"]),
            old_string=str(args.get("old_string") or ""),
            new_string=str(args.get("new_string") or ""),
            replace_all=bool(args.get("replace_all") or False),
        ),
        need_confirm=True,
        approval_preview=lambda args: _edit_approval_preview(policy, args),
    )


def write_path(
    policy: FileMutationAccessPolicy,
    *,
    path: str,
    content: str,
    mode: str = "overwrite",
) -> ToolResult:
    mode = str(mode or "overwrite")
    if mode not in {"overwrite", "append"}:
        return ToolResult(output="mode must be 'overwrite' or 'append'.", is_error=True)

    try:
        file_path = _resolve_mutation_path(policy, path)
    except ValueError as error:
        return ToolResult(output=str(error), is_error=True)

    parent_error = _parent_directory_error(file_path)
    if parent_error:
        return ToolResult(output=parent_error, is_error=True)
    if file_path.exists() and not file_path.is_file():
        return ToolResult(output=f"{path} is not a file.", is_error=True)
    final_content = content
    if mode == "append" and file_path.exists():
        try:
            final_content = file_path.read_text(encoding="utf-8") + content
        except UnicodeDecodeError:
            return ToolResult(output=f"{path} is not readable as UTF-8 text.", is_error=True)
        except OSError as error:
            return ToolResult(output=f"Failed to read {path}: {error}", is_error=True)
    validation_error = _memory_file_validation_error(policy, file_path, final_content)
    if validation_error:
        return ToolResult(output=validation_error, is_error=True)

    try:
        with file_path.open(
            "a" if mode == "append" else "w",
            encoding="utf-8",
            newline="",
        ) as handle:
            handle.write(content)
    except OSError as error:
        return ToolResult(output=f"Failed to write {path}: {error}", is_error=True)

    byte_count = len(content.encode("utf-8"))
    verb = "Appended" if mode == "append" else "Wrote"
    return ToolResult(output=f"{verb} {byte_count} bytes to {path}")


def edit_path(
    policy: FileMutationAccessPolicy,
    *,
    path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> ToolResult:
    if not old_string:
        return ToolResult(output="old_string cannot be empty.", is_error=True)
    if old_string == new_string:
        return ToolResult(
            output="No changes to make: old_string and new_string are exactly the same.",
            is_error=True,
        )

    try:
        file_path = _resolve_mutation_path(policy, path)
    except ValueError as error:
        return ToolResult(output=str(error), is_error=True)
    if not file_path.exists():
        return ToolResult(output=f"{path} does not exist.", is_error=True)
    if not file_path.is_file():
        return ToolResult(output=f"{path} is not a file.", is_error=True)

    try:
        with file_path.open("r", encoding="utf-8", errors="strict", newline="") as handle:
            raw_content = handle.read()
    except UnicodeDecodeError:
        return ToolResult(output=f"{path} is not readable as UTF-8 text.", is_error=True)
    except OSError as error:
        return ToolResult(output=f"Failed to read {path}: {error}", is_error=True)

    model_view = _to_model_text_view(raw_content)
    content = model_view.text
    if replace_all:
        replacement_count = content.count(old_string)
        if replacement_count == 0:
            return ToolResult(output=_old_string_not_found(path), is_error=True)
        new_content = content.replace(old_string, new_string)
    else:
        replacement_count = content.count(old_string)
        if replacement_count == 0:
            return ToolResult(output=_old_string_not_found(path), is_error=True)
        if replacement_count > 1:
            return ToolResult(
                output=(
                    f"old_string is not unique in {path} "
                    f"(found {replacement_count} occurrences). To replace every occurrence, "
                    "set replace_all=true. To replace only one occurrence, include more "
                    "surrounding context in old_string."
                ),
                is_error=True,
            )
        new_content = content.replace(old_string, new_string, 1)

    materialized = _materialize_model_text(
        new_content,
        line_ending_style=model_view.line_ending_style,
    )
    validation_error = _memory_file_validation_error(policy, file_path, materialized)
    if validation_error:
        return ToolResult(output=validation_error, is_error=True)
    try:
        with file_path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(materialized)
    except OSError as error:
        return ToolResult(output=f"Failed to write {path}: {error}", is_error=True)

    word = "occurrence" if replacement_count == 1 else "occurrences"
    return ToolResult(output=f"Replaced {replacement_count} {word} in {path}")


@dataclass(frozen=True)
class _ModelTextView:
    text: str
    line_ending_style: str


def _to_model_text_view(raw: str) -> _ModelTextView:
    style = _detect_line_ending_style(raw)
    if style == "crlf":
        return _ModelTextView(text=raw.replace("\r\n", "\n"), line_ending_style=style)
    return _ModelTextView(text=raw, line_ending_style=style)


def _materialize_model_text(text: str, *, line_ending_style: str) -> str:
    if line_ending_style != "crlf":
        return text
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def _detect_line_ending_style(text: str) -> str:
    has_crlf = False
    has_lf = False
    has_lone_cr = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\r":
            if index + 1 < len(text) and text[index + 1] == "\n":
                has_crlf = True
                index += 2
                continue
            has_lone_cr = True
        elif char == "\n":
            has_lf = True
        index += 1
    if has_lone_cr or (has_crlf and has_lf):
        return "mixed"
    if has_crlf:
        return "crlf"
    return "lf"


def _old_string_not_found(path: str) -> str:
    return (
        f"old_string not found in {path}, the file contents may be out of date. "
        "Please use the Read Tool to reload the content."
    )


def _parent_directory_error(file_path: Path) -> str:
    parent = file_path.parent
    if not parent.exists():
        return f"Parent directory does not exist: {parent}. Create it before writing this file."
    if not parent.is_dir():
        return f"Parent path is not a directory: {parent}."
    return ""


def _resolve_mutation_path(policy: FileMutationAccessPolicy, path: str) -> Path:
    raw_path = str(path or "").strip()
    if not raw_path:
        raise ValueError("Path is required")
    if is_memory_space_uri(raw_path):
        return _resolve_memory_space_mutation_path(policy, raw_path)

    parsed = urlparse(raw_path)
    if parsed.scheme in {"http", "https"}:
        raise ValueError("Write/Edit only accept local file paths or file:// URIs")
    if parsed.scheme and parsed.scheme != "file":
        raise ValueError(f"Write/Edit do not support URI scheme: {parsed.scheme}")
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
    _ensure_memory_space_real_path_mutation_allowed(policy, resolved)
    for root in policy.allowed_roots:
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            continue
        _ensure_path_allowed(relative, policy)
        return resolved
    raise ValueError("Path must stay inside an allowed Write/Edit root")


def _ensure_memory_space_real_path_mutation_allowed(
    policy: FileMutationAccessPolicy,
    resolved: Path,
) -> None:
    if policy.memory_space_root is None:
        return
    layout = MemorySpaceLayout.for_root(policy.memory_space_root)
    root = layout.root.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        return
    memory_root = layout.memory_dir.resolve(strict=False)
    if not _is_relative_to(resolved, memory_root):
        raise ValueError("Write/Edit only support memory space memory/ paths")


def _memory_file_validation_error(
    policy: FileMutationAccessPolicy,
    file_path: Path,
    content: str,
) -> str:
    if policy.memory_space_root is None:
        return ""
    layout = MemorySpaceLayout.for_root(policy.memory_space_root)
    memory_root = layout.memory_dir.resolve(strict=False)
    resolved = file_path.resolve(strict=False)
    if not _is_relative_to(resolved, memory_root):
        return ""
    return validate_memory_file_content(
        content,
        path=resolved,
        memory_dir=memory_root,
    )


def _resolve_memory_space_mutation_path(
    policy: FileMutationAccessPolicy,
    uri: str,
) -> Path:
    if policy.memory_space_root is None:
        raise ValueError("Write/Edit require memory_space_dir for memory_space:// URIs")
    relative = memory_space_relative_path(uri)
    if not (relative == "memory" or relative.startswith("memory/")):
        raise ValueError("Write/Edit only support memory_space://memory/* paths")
    layout = MemorySpaceLayout.for_root(policy.memory_space_root)
    return layout.root / relative


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _ensure_path_allowed(
    relative_path: Path,
    policy: FileMutationAccessPolicy,
) -> None:
    if is_denied_by_patterns(relative_path, policy.deny_patterns):
        raise ValueError(f"Cannot write denied file: {relative_path}")
    if not policy.default_denies:
        return
    if any(part in BLOCKED_PARTS for part in relative_path.parts):
        raise ValueError(f"Cannot write blocked path: {relative_path}")
    if _is_sensitive_file(relative_path):
        raise ValueError(f"Cannot write sensitive file: {relative_path}")


def _file_mutation_access_policy(
    *,
    repo_root: Path | None,
    data_dir: Path | None,
    memory_space_dir: Path | None,
    options: dict[str, Any],
) -> FileMutationAccessPolicy:
    roots = configured_allowed_roots(
        options,
        repo_root=repo_root,
        data_dir=data_dir,
        option_label="Write/Edit allowed path",
    ) or _default_allowed_roots(repo_root=repo_root, data_dir=data_dir)
    return FileMutationAccessPolicy(
        allowed_roots=roots,
        deny_patterns=tuple(configured_deny_patterns(options)),
        default_denies=_option_bool(
            options,
            ("default_denies",),
            default=True,
        ),
        memory_space_root=memory_space_dir.expanduser().resolve()
        if memory_space_dir is not None
        else None,
    )


def _default_allowed_roots(
    *,
    repo_root: Path | None,
    data_dir: Path | None,
) -> list[Path]:
    roots: list[Path] = []
    for root in (repo_root, data_dir):
        if root is None:
            continue
        resolved = root.expanduser().resolve()
        if resolved not in roots:
            roots.append(resolved)
    if not roots:
        raise ValueError("Write/Edit require repo_root or data_dir")
    return roots


def _option_bool(
    options: dict[str, Any],
    keys: tuple[str, ...],
    *,
    default: bool,
) -> bool:
    value: object = None
    for key in keys:
        if options.get(key) is not None:
            value = options.get(key)
            break
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
        if not name.startswith(prefix):
            continue
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


def _write_approval_preview(
    policy: FileMutationAccessPolicy,
    args: dict[str, Any],
) -> str:
    path = _resolve_mutation_path(policy, str(args.get("path") or ""))
    mode = str(args.get("mode") or "overwrite")
    content = str(args.get("content") or "")
    byte_count = len(content.encode("utf-8"))
    preview = _truncate_preview(content)
    lines = [
        f"mode: {mode}",
        f"path: {path}",
        f"bytes: {byte_count}",
    ]
    if preview:
        lines.extend(["content:", preview])
    return "\n".join(lines)


def _edit_approval_preview(
    policy: FileMutationAccessPolicy,
    args: dict[str, Any],
) -> str:
    path = _resolve_mutation_path(policy, str(args.get("path") or ""))
    old_string = str(args.get("old_string") or "")
    new_string = str(args.get("new_string") or "")
    lines = [
        f"path: {path}",
        f"replace_all: {bool(args.get('replace_all') or False)}",
        "old_string:",
        _truncate_preview(old_string),
        "new_string:",
        _truncate_preview(new_string),
    ]
    return "\n".join(lines)


def _truncate_preview(text: str) -> str:
    rendered = str(text or "").replace("\r", "\\r")
    if len(rendered) <= APPROVAL_PREVIEW_CHARS:
        return rendered
    return f"{rendered[: APPROVAL_PREVIEW_CHARS - 3]}..."
