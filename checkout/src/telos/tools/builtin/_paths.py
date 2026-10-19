"""Shared path/pattern matching helpers for the built-in file tools."""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from telos.tools.builtin._options import option_list, resolve_env_ref


def resolve_configured_path(
    value: str,
    *,
    repo_root: Path | None,
    data_dir: Path | None,
    default_base: Path | None,
    option_label: str,
) -> Path:
    """Resolve one configured path, expanding ``repo``/``data`` aliases and env refs."""

    text = resolve_env_ref(str(value or "").strip())
    if not text:
        raise ValueError(f"{option_label} cannot be empty")
    if text in {"repo", "repo_root"} and repo_root is not None:
        return repo_root.expanduser().resolve()
    if text in {"data", "data_dir"} and data_dir is not None:
        return data_dir.expanduser().resolve()
    path = Path(text).expanduser()
    if not path.is_absolute():
        if default_base is None:
            raise ValueError(f"Relative {option_label} requires repo_root or data_dir")
        path = default_base / path
    return path.resolve()


def configured_allowed_roots(
    options: dict[str, Any],
    *,
    repo_root: Path | None,
    data_dir: Path | None,
    default_base: Path | None = None,
    option_label: str,
) -> list[Path]:
    roots: list[Path] = []
    for value in option_list(options, "allow"):
        root = resolve_configured_path(
            value,
            repo_root=repo_root,
            data_dir=data_dir,
            default_base=default_base if default_base is not None else (repo_root or data_dir),
            option_label=option_label,
        )
        if root not in roots:
            roots.append(root)
    return roots


def configured_deny_patterns(options: dict[str, Any]) -> list[str]:
    return option_list(options, "deny")


def is_denied_by_patterns(relative_path: Path, patterns: tuple[str, ...]) -> bool:
    denied = False
    for raw_pattern in patterns:
        pattern = str(raw_pattern or "").strip()
        if not pattern or pattern.startswith("#"):
            continue
        negated = pattern.startswith("!")
        if negated:
            pattern = pattern[1:].strip()
        if not pattern:
            continue
        if matches_ignore_pattern(relative_path, pattern):
            denied = not negated
    return denied


def matches_ignore_pattern(relative_path: Path, pattern: str) -> bool:
    parts = [part for part in relative_path.parts if part not in {"", "."}]
    if not parts:
        return False
    directory_only = pattern.endswith("/")
    pattern = pattern.rstrip("/")
    anchored = pattern.startswith("/")
    pattern = pattern.lstrip("/")
    if not pattern:
        return False

    if "/" not in pattern:
        names = parts[:-1] if directory_only else parts
        return any(fnmatchcase(part, pattern) for part in names)

    path_text = "/".join(parts)
    candidates = ["/".join(parts[:index]) for index in range(1, len(parts))]
    if not directory_only:
        candidates.append(path_text)
    if anchored:
        patterns = [pattern]
    else:
        patterns = [pattern, f"*/{pattern}", f"**/{pattern}"]
    return any(
        fnmatchcase(candidate, item)
        for candidate in candidates
        for item in patterns
    )
