"""Single-instance lock for the worker, so a logon task, the hourly watchdog and a manual `agent run`
can never run two schedulers (which would double-apply). The OS releases the lock if the process dies."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from agent import paths


class AlreadyRunning(Exception):
    pass


def lock_path() -> Path:
    return paths.agent_home() / "data" / "worker.lock"


def is_locked(path: Path | None = None) -> bool:
    """Check if the lock is held without acquiring it."""
    path = path or lock_path()
    if not path.exists():
        return False
    try:
        handle = open(path, "a+")
    except OSError:
        return True
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(4096)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        handle.close()


@contextmanager
def single_instance(path: Path | None = None) -> Iterator[None]:
    path = path or lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")  # noqa: SIM115 - held for the life of the worker
    try:
        try:
            if os.name == "nt":
                import msvcrt

                # Lock a byte past the content so the PID stays readable by other processes.
                handle.seek(4096)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AlreadyRunning(str(path)) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        handle.close()
