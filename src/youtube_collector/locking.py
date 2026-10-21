"""Single collector per database, using local POSIX advisory flock."""

import fcntl
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_OWNED = ContextVar("collector_owned_locks", default=())


class CollectorAlreadyRunning(RuntimeError):
    """Another process/thread owns this database's collector lock."""


@contextmanager
def collector_lock(db_path: str | Path):
    database = Path(db_path).resolve()
    identity = (str(database), os.getpid(), threading.get_ident())
    owned = _OWNED.get()
    if identity in owned:  # main holds the run lock; collect_video can nest safely.
        yield
        return
    database.parent.mkdir(parents=True, exist_ok=True)
    lock_path = database.with_name(database.name + ".collector.lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CollectorAlreadyRunning("Колектор уже працює з цією базою. Повторіть запуск після його завершення.") from None
        token = _OWNED.set((*owned, identity))
        try:
            yield
        finally:
            _OWNED.reset(token)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
    # Never unlink: replacing a lock inode can let two processes own different locks.
