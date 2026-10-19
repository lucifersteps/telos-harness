"""Storage paths."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TelosPaths:
    repo_root: Path
    config_dir: Path
    config_path: Path
    root_dir: Path
    main_config_path: Path
    agent_id: str
    agent_config_path: Path
    config_base_dir: Path
    memory_space_name: str
    memory_space_dir: Path
    agent_dir: Path
    sources_dir: Path
    indexes_dir: Path
    sessions_dir: Path

    @classmethod
    def from_root(
        cls,
        repo_root: Path,
        *,
        config_dir: Path | None = None,
        config_path: Path | None = None,
        root_dir: Path | None = None,
        main_config_path: Path | None = None,
        agent_id: str = "default",
        agent_config_path: Path | None = None,
        memory_space: str = "defaults",
    ) -> "TelosPaths":
        root = repo_root.resolve()
        resolved_config = (config_dir or root / "configs").resolve()
        resolved_config_path = (
            config_path.expanduser().resolve()
            if config_path is not None
            else resolved_config / "telos.yaml"
        )
        resolved_root = (root_dir or root).expanduser().resolve()
        resolved_main_config_path = (
            main_config_path.expanduser().resolve()
            if main_config_path is not None
            else resolved_config_path
        )
        clean_agent_id = _clean_segment(agent_id)
        resolved_agent_config_path = (
            agent_config_path.expanduser().resolve()
            if agent_config_path is not None
            else resolved_config_path
        )
        agent_dir = resolved_root / "agents" / clean_agent_id
        indexes_dir = agent_dir / "indexes"
        memory_space_name = clean_memory_space_name(memory_space)
        resolved_memory_space_dir = resolved_root / "memory_spaces" / memory_space_name
        return cls(
            repo_root=root,
            config_dir=resolved_config,
            config_path=resolved_config_path,
            root_dir=resolved_root,
            main_config_path=resolved_main_config_path,
            agent_id=clean_agent_id,
            agent_config_path=resolved_agent_config_path,
            config_base_dir=resolved_agent_config_path.parent,
            memory_space_name=memory_space_name,
            memory_space_dir=resolved_memory_space_dir,
            agent_dir=agent_dir,
            sources_dir=agent_dir / "sources",
            indexes_dir=indexes_dir,
            sessions_dir=agent_dir / "sessions",
        )


def default_source_root() -> Path:
    """Root that holds the bundled ``profiles/`` / ``skills/`` / ``configs/``.

    Frozen (PyInstaller one-file): the ``_MEIPASS`` extraction dir, where
    ``--add-data`` places these trees. In that layout the ``src/`` level is
    dropped, so ``parents[N]`` math against ``__file__`` overshoots — this
    helper is the single source of truth instead. Otherwise: the repo root
    inferred from this file's location (cwd-independent).
    """

    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        return Path(str(meipass)).resolve()
    # src/telos/storage/paths.py → parents[3] == repo root
    return Path(__file__).resolve().parents[3]


def find_repo_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "pyproject.toml").exists() and (candidate / "src").exists():
            return candidate
        if (candidate / "configs").is_dir() and (candidate / "src" / "telos").is_dir():
            return candidate
    return current


def _clean_segment(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "default"
    safe = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in text)
    return safe.strip("._-") or "default"


def clean_memory_space_name(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "defaults"
    if Path(text).expanduser().is_absolute() or "/" in text or "\\" in text:
        raise ValueError("memory_space must be a name, not a path")
    if text in {".", ".."} or text.startswith("."):
        raise ValueError("memory_space must be a stable name")
    safe = "".join(char if char.isalnum() or char in {"-", "_", "."} else "" for char in text)
    if safe != text or not safe.strip("._-"):
        raise ValueError("memory_space must contain only letters, numbers, '-', '_' or '.'")
    return text
