import importlib
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

CURRENT_RELEASE = Path("/opt/pyracast/current")
RELEASE_BLOCKED = Path("/var/lib/pyracast/release-blocked")
RELEASE_LOCK = Path("/run/lock/pyracast-release.lock")
LAST_BACKUP = Path("/var/lib/pyracast/last-backup.json")
# Covers the hourly timer, its 300 s randomized delay and the backup's 600 s lock wait.
BACKUP_MAX_AGE_SECONDS = 90 * 60


def try_lock(stream: IO[str]) -> bool:
    locks = importlib.import_module("fcntl")
    try:
        locks.flock(stream, locks.LOCK_EX | locks.LOCK_NB)
    except BlockingIOError:
        return False
    return True


@contextmanager
def pointer_lock() -> Iterator[None]:
    locks = importlib.import_module("fcntl")
    with LAST_BACKUP.with_suffix(".lock").open("a") as stream:
        locks.flock(stream, locks.LOCK_EX)
        yield


@contextmanager
def release_lock(wait_seconds: float) -> Iterator[bool]:
    deadline = time.monotonic() + wait_seconds
    with RELEASE_LOCK.open("a") as stream:
        while not try_lock(stream):
            if time.monotonic() >= deadline:
                yield False
                return
            time.sleep(1)
        yield True
