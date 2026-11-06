"""One file lock per task, shared by the panel and the scheduler (plan T1).

The panel and the scheduler run in separate containers that share `/data`, so the lock is a
file under `<DATA_DIR>/locks/<task>.lock`, held with `fcntl.flock` by the task process for its
whole run. The kernel frees it when the process ends, even after a crash, so there are no stale
locks to clean up.

Two ways to get it:
- `task_lock(data_dir, name)` in the task itself (scheduler start, or `python -m` by hand).
- `acquire(...)` in the panel, then the open file is handed to the new process (`TASK_LOCK_FD`).
  The task then takes that already-locked file, so there is no gap between the click and the
  lock. The panel closes its own copy; the lock lives as long as the task process.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

ENV_FD = "TASK_LOCK_FD"


class TaskBusy(Exception):
    """Another process holds this task's lock."""


def lock_path(data_dir: Path, name: str) -> Path:
    return Path(data_dir) / "locks" / f"{name}.lock"


def _stamp(fd: int) -> None:
    """Write 'start time, pid' into the lock file so the panel can show 'running since'."""
    text = f"{datetime.now(UTC).isoformat(timespec='seconds')}\n{os.getpid()}\n".encode()
    os.ftruncate(fd, 0)
    os.pwrite(fd, text, 0)


def acquire(data_dir: Path, name: str) -> int:
    """Take the lock without waiting. Returns the open file descriptor. Raises TaskBusy."""
    path = lock_path(data_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise TaskBusy(name) from None
    _stamp(fd)
    return fd


def _inherited(data_dir: Path, name: str) -> int | None:
    """The lock the panel handed over, if it is really this task's lock file."""
    raw = os.environ.pop(ENV_FD, "")
    if not raw.isdigit():
        return None
    fd = int(raw)
    try:
        same = os.path.samestat(os.fstat(fd), os.stat(lock_path(data_dir, name)))
    except OSError:
        return None
    if not same:
        return None
    os.set_inheritable(fd, False)  # Claude and other children must not keep the lock alive
    _stamp(fd)
    return fd


@contextmanager
def task_lock(data_dir: Path, name: str) -> Iterator[None]:
    """Hold the task lock for the whole `with` block. Raises TaskBusy if it is taken."""
    fd = _inherited(data_dir, name)
    if fd is None:
        fd = acquire(data_dir, name)
    try:
        yield
    finally:
        os.close(fd)  # closing the file releases the lock


def running_since(data_dir: Path, name: str) -> str | None:
    """None if the task is not running. Else the start time (ISO), or '' if not written yet."""
    path = lock_path(data_dir, name)
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            try:
                return os.read(fd, 200).decode(errors="replace").splitlines()[0]
            except IndexError:
                return ""
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)
