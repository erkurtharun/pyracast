import importlib
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

CURRENT_RELEASE = Path("/opt/pyracast/current")
RELEASE_BLOCKED = Path("/var/lib/pyracast/release-blocked")
RELEASE_LOCK = Path("/run/lock/pyracast-release.lock")
LAST_BACKUP = Path("/var/lib/pyracast/last-backup.json")
PRERELEASE_RECORD = "prerelease-backup.json"
# The gateway's loopback entrypoint serves the public certificate without asking for the edge's.
LOCAL_HTTPS_PORT = 8443
# Covers the hourly timer, its 300 s randomized delay and the backup's 600 s lock wait.
BACKUP_MAX_AGE_SECONDS = 90 * 60


def try_lock(stream: IO[str]) -> bool:
    locks = importlib.import_module("fcntl")
    try:
        locks.flock(stream, locks.LOCK_EX | locks.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def holds_release_lock(descriptor: int) -> bool:
    try:
        held = os.fstat(descriptor)
        lock = RELEASE_LOCK.stat()
    except OSError:
        return False
    if not os.path.samestat(held, lock):
        return False
    with RELEASE_LOCK.open("a") as probe:
        if try_lock(probe):
            return False
    # flock grants a lock again to the open file description that already holds it.
    locks = importlib.import_module("fcntl")
    try:
        locks.flock(descriptor, locks.LOCK_EX | locks.LOCK_NB)
    except BlockingIOError:
        return False
    return True


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
