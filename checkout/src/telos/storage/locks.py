"""Storage locks."""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


DEFAULT_LOCK_TIMEOUT_SECONDS = 10.0
DEFAULT_LOCK_POLL_SECONDS = 0.05


class FileLockTimeout(TimeoutError):
    """Raised when a storage file lock cannot be acquired in time."""


@contextmanager
def file_lock(
    target: Path,
    *,
    timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
    poll_seconds: float = DEFAULT_LOCK_POLL_SECONDS,
) -> Iterator[None]:
    lock_path = _lock_path_for(target)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        _acquire_lock(
            handle,
            target=target,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
        )
        try:
            yield
        finally:
            _release_lock(handle)
    finally:
        handle.close()


def _lock_path_for(target: Path) -> Path:
    resolved = str(target.expanduser().resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()
    return Path(tempfile.gettempdir()) / "telos-locks" / f"{digest}.lock"


def _acquire_lock(
    handle,
    *,
    target: Path,
    timeout_seconds: float,
    poll_seconds: float,
) -> None:
    deadline = time.monotonic() + max(timeout_seconds, 0)
    while True:
        if _try_lock(handle):
            return
        if time.monotonic() >= deadline:
            raise FileLockTimeout(f"Timed out acquiring storage lock for {target}")
        time.sleep(max(poll_seconds, 0.001))


def _try_lock(handle) -> bool:
    if os.name == "nt":
        return _try_lock_windows(handle)
    return _try_lock_posix(handle)


def _release_lock(handle) -> None:
    if os.name == "nt":
        _release_lock_windows(handle)
    else:
        _release_lock_posix(handle)


def _try_lock_posix(handle) -> bool:
    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _release_lock_posix(handle) -> None:
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _try_lock_windows(handle) -> bool:
    import msvcrt

    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _release_lock_windows(handle) -> None:
    import msvcrt

    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
