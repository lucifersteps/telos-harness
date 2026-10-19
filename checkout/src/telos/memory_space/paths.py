"""Filesystem layout and resolver for memory_space:// URIs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from telos.memory_space.memory_files import MEMORY_ENTRY_FILENAME
from telos.storage.filesystem import ensure_dir


MEMORY_SPACE_SCHEME = "memory_space"


@dataclass(frozen=True)
class MemorySpaceLayout:
    root: Path
    runtime_timeline_screenshots_dir: Path | None = None

    @classmethod
    def for_root(
        cls,
        root: Path,
        *,
        runtime_timeline_screenshots_dir: Path | None = None,
    ) -> "MemorySpaceLayout":
        return cls(
            Path(root).expanduser().resolve(),
            Path(runtime_timeline_screenshots_dir).expanduser().resolve()
            if runtime_timeline_screenshots_dir is not None
            else None,
        )

    @property
    def memory_dir(self) -> Path:
        return self.root / "memory"

    @property
    def memory_entry_path(self) -> Path:
        return self.memory_dir / MEMORY_ENTRY_FILENAME

    @property
    def context_dir(self) -> Path:
        return self.root / "context"

    @property
    def context_db_path(self) -> Path:
        return self.context_dir / "context.sqlite"

    @property
    def attachments_dir(self) -> Path:
        return self.context_dir / "attachments"

    @property
    def recaps_dir(self) -> Path:
        return self.root / "recaps"

    @property
    def recap_db_path(self) -> Path:
        return self.recaps_dir / "recaps.sqlite"

    @property
    def recap_runs_dir(self) -> Path:
        return self.recaps_dir / "runs"

    @property
    def knowledge_dir(self) -> Path:
        return self.root / "knowledge"

    @property
    def knowledge_db_path(self) -> Path:
        return self.knowledge_dir / "knowledge.sqlite"

    @property
    def timeline_dir(self) -> Path:
        return self.root / "timeline"

    @property
    def timeline_db_path(self) -> Path:
        return self.timeline_dir / "timeline.sqlite"

    @property
    def timeline_screenshots_dir(self) -> Path:
        return self.timeline_dir / "screenshots"

    @property
    def timeline_runs_dir(self) -> Path:
        return self.timeline_dir / "runs"

    def ensure(self) -> None:
        for path in (
            self.memory_dir,
            self.context_dir,
            self.attachments_dir,
            self.recaps_dir,
            self.recap_runs_dir,
            self.knowledge_dir,
            self.timeline_dir,
            self.timeline_screenshots_dir,
            self.timeline_runs_dir,
        ):
            ensure_dir(path)
        _ensure_empty_file(self.memory_entry_path)

    def uri(self, relative_path: str) -> str:
        clean = _clean_relative_path(relative_path)
        return f"{MEMORY_SPACE_SCHEME}://{clean}"


class MemorySpaceResolver:
    def __init__(self, layout: MemorySpaceLayout):
        self.layout = layout

    def resolve(self, uri: str) -> Path:
        relative = memory_space_relative_path(uri)
        if not _is_visible_memory_space_relative_path(relative):
            raise ValueError("memory_space URI is outside visible roots")
        return self.layout.root / relative


@dataclass(frozen=True)
class AgentRuntimeLayout:
    root: Path

    @classmethod
    def for_agent_dir(cls, agent_dir: Path) -> "AgentRuntimeLayout":
        return cls(Path(agent_dir).expanduser().resolve())

    @property
    def runtime_dir(self) -> Path:
        return self.root / "runtime"

    @property
    def runtime_timeline_dir(self) -> Path:
        return self.runtime_dir / "timeline"

    @property
    def runtime_timeline_db_path(self) -> Path:
        return self.runtime_timeline_dir / "collector.sqlite"

    @property
    def runtime_timeline_screenshots_dir(self) -> Path:
        return self.runtime_timeline_dir / "screenshots"

    @property
    def runtime_activity_db_path(self) -> Path:
        return self.runtime_dir / "activity.sqlite"

    @property
    def runtime_adapters_dir(self) -> Path:
        return self.runtime_dir / "adapters"

    @property
    def runtime_wechat_dir(self) -> Path:
        return self.runtime_adapters_dir / "wechat"

    @property
    def runtime_wechat_accounts_dir(self) -> Path:
        return self.runtime_wechat_dir / "accounts"

    @property
    def runtime_wechat_cache_dir(self) -> Path:
        return self.runtime_wechat_dir / "cache"

    @property
    def runtime_wire_path(self) -> Path:
        return self.runtime_dir / "wire.jsonl"

    @property
    def runtime_locks_dir(self) -> Path:
        return self.runtime_dir / "locks"

    def ensure(self) -> None:
        for path in (
            self.runtime_dir,
            self.runtime_timeline_dir,
            self.runtime_timeline_screenshots_dir,
            self.runtime_adapters_dir,
            self.runtime_wechat_dir,
            self.runtime_wechat_accounts_dir,
            self.runtime_wechat_cache_dir,
            self.runtime_locks_dir,
        ):
            ensure_dir(path)


def agent_dir_for_session_dir(session_dir: Path) -> Path:
    resolved = Path(session_dir).expanduser().resolve()
    if resolved.parent.name == "sessions":
        return resolved.parent.parent
    return resolved


def memory_space_root_for_session(session: object) -> Path:
    metadata = getattr(session, "metadata", None)
    raw = str(getattr(metadata, "memory_space_dir", "") or "").strip()
    if not raw:
        raise ValueError("session metadata is missing memory_space_dir")
    return Path(raw).expanduser().resolve()


def is_memory_space_uri(uri: str) -> bool:
    return str(uri or "").strip().startswith(f"{MEMORY_SPACE_SCHEME}://")


def memory_space_relative_path(uri: str) -> str:
    text = str(uri or "").strip()
    prefix = f"{MEMORY_SPACE_SCHEME}://"
    if not text.startswith(prefix):
        raise ValueError("expected memory_space:// URI")
    return _clean_relative_path(unquote(text[len(prefix) :]))


def _clean_relative_path(value: str) -> str:
    text = str(value or "").strip().lstrip("/")
    path = PurePosixPath(text)
    if not text or path.is_absolute():
        raise ValueError("memory_space path must be relative")
    parts = path.parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("memory_space path contains unsafe segment")
    return "/".join(parts)


def _is_visible_memory_space_relative_path(relative: str) -> bool:
    return any(
        relative == prefix or relative.startswith(f"{prefix}/")
        for prefix in ("memory", "context", "recaps", "timeline", "knowledge")
    )


def _ensure_empty_file(path: Path) -> None:
    try:
        with path.open("x", encoding="utf-8"):
            pass
    except FileExistsError:
        if not path.is_file():
            raise
