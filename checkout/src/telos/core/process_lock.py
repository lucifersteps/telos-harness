"""Cross-process single-instance lock backed by an advisory file lock.

The lock is held for the lifetime of the owning process via an open file
descriptor. The kernel releases it automatically when that process exits by any
means — clean exit, crash, ``SIGKILL``, or power loss — so there is no stale
lock to clean up (unlike a hand-rolled PID file). This makes it the right
primitive for enforcing "at most one instance of X" across independently spawned
processes, e.g. the timeline collector, which may be launched by more than one
daemon (CLI + embedded web) and can outlive its parent as an orphan.

Cooperating processes must all point at the same dedicated lock file — never a
file that is later truncated/recreated, since the lock follows the open file
description, not the path.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]


class SingleInstanceLock:
    """Non-blocking advisory lock on a dedicated lock file.

    Usage::

        lock = SingleInstanceLock(path)
        if not lock.acquire():
            return  # another instance owns it
        try:
            ...  # run
        finally:
            lock.release()
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    @property
    def owner_pid_path(self) -> Path:
        return _pid_path_for(self.path)

    def owner_pid(self) -> int | None:
        return _read_owner_pid(self.owner_pid_path)

    def acquire(self) -> bool:
        """Try to grab the lock without blocking.

        Returns ``True`` if this process now owns it, ``False`` if another live
        process already holds it. On platforms without ``fcntl`` locking is a
        no-op and always succeeds.
        """
        if self._fd is not None:
            return True
        if fcntl is None:  # pragma: no cover - non-POSIX platforms
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        _write_owner_pid(self.owner_pid_path)
        return True

    def release(self) -> None:
        """Release the lock if held. Idempotent."""
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            if fcntl is not None:  # pragma: no branch
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - best-effort unlock
            logger.debug("failed to release single-instance lock %s", self.path, exc_info=True)
        finally:
            os.close(fd)
            _clear_owner_pid(self.owner_pid_path)

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def _pid_path_for(lock_path: Path) -> Path:
    return lock_path.with_name(lock_path.stem + ".pid")


def _read_owner_pid(pid_path: Path) -> int | None:
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _write_owner_pid(pid_path: Path) -> None:
    try:
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:  # pragma: no cover - best effort
        logger.debug("failed to write owner pid file %s", pid_path, exc_info=True)


def _clear_owner_pid(pid_path: Path) -> None:
    try:
        current = _read_owner_pid(pid_path)
        if current == os.getpid():
            pid_path.unlink()
    except OSError:  # pragma: no cover - best effort
        logger.debug("failed to clear owner pid file %s", pid_path, exc_info=True)


def pid_alive(pid: int) -> bool:
    """True when ``pid`` names a live process (including one we cannot signal)."""

    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - exists but not ours
        return True
    return True


def acquire_or_replace(
    lock_path: Path | str,
    *,
    replace: bool,
    term_wait_seconds: float = 5.0,
    kill_wait_seconds: float = 3.0,
    owner_label: str = "Telos 后端",
    log: logging.Logger = logger,
) -> SingleInstanceLock | None:
    """Grab the single-instance lock, optionally evicting a stale live holder.

    Returns the held lock on success. If another live instance holds it and
    ``replace`` is False, returns ``None`` (caller should refuse to start). If
    ``replace`` is True, the current holder (read from the sibling ``.pid`` file)
    is asked to exit — ``SIGTERM`` first, ``SIGKILL`` as a last resort — and the
    lock is re-acquired once the kernel releases it on that process's exit.

    Used by the desktop sidecar (``--replace``) so a freshly launched backend
    reliably takes over an orphaned one left behind by a crashed/force-killed
    shell, guaranteeing at most one backend per agent (no sqlite contention).
    """

    lock_path = Path(lock_path)
    pid_path = _pid_path_for(lock_path)
    lock = SingleInstanceLock(lock_path)

    if lock.acquire():
        _write_owner_pid(pid_path)
        return lock
    if not replace:
        return None

    old_pid = _read_owner_pid(pid_path)
    if old_pid and old_pid != os.getpid() and pid_alive(old_pid):
        log.warning("已有 %s pid=%s 持有该配置，正在接管（SIGTERM）", owner_label, old_pid)
        try:
            os.kill(old_pid, signal.SIGTERM)
        except OSError:
            pass

    if _poll_acquire(lock, term_wait_seconds):
        _write_owner_pid(pid_path)
        return lock

    if old_pid and pid_alive(old_pid):
        log.warning("%s pid=%s 未在 %.0fs 内退出，强制结束（SIGKILL）", owner_label, old_pid, term_wait_seconds)
        try:
            os.kill(old_pid, signal.SIGKILL)
        except OSError:
            pass

    if _poll_acquire(lock, kill_wait_seconds):
        _write_owner_pid(pid_path)
        return lock
    return None


def _poll_acquire(lock: SingleInstanceLock, wait_seconds: float) -> bool:
    deadline = time.monotonic() + max(wait_seconds, 0)
    while True:
        if lock.acquire():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
