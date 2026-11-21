import importlib
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

CURRENT_RELEASE = Path("/opt/pyracast/current")
RELEASE_BLOCKED = Path("/var/lib/pyracast/release-blocked")
RELEASE_LOCK = Path("/var/lib/pyracast/release.lock")
REHEARSAL_LOCK = Path("/var/lib/pyracast/rehearsal.lock")
LAST_BACKUP = Path("/var/lib/pyracast/last-backup.json")
LAST_REHEARSAL = Path("/var/lib/pyracast/last-rehearsal.json")
# The recovery helper's archives and session, on the data disk beside PostgreSQL.
RECOVERY_STATE = Path("/var/lib/pyracast/reset")
PRERELEASE_RECORD = "prerelease-backup.json"
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
UPTIME = Path("/proc/uptime")
# The gateway's loopback entrypoint serves the public certificate without asking for the edge's.
LOCAL_HTTPS_PORT = 8443
# Covers the hourly timer, its 300 s randomized delay and the backup's 600 s lock wait.
BACKUP_MAX_AGE_SECONDS = 90 * 60
# The weekly timer, its 30-minute randomized delay and the rehearsal's one-hour deadline.
REHEARSAL_MAX_AGE_SECONDS = 8 * 86400


def open_lock(path: Path) -> IO[str]:
    # O_NOFOLLOW exists only on POSIX, where the helpers run.
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    return os.fdopen(os.open(path, flags, 0o600), "a")


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
    with open_lock(RELEASE_LOCK) as probe:
        if try_lock(probe):
            return False
    # flock grants a lock again to the open file description that already holds it.
    locks = importlib.import_module("fcntl")
    try:
        locks.flock(descriptor, locks.LOCK_EX | locks.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def boot_clock() -> tuple[str, float]:
    return BOOT_ID.read_text().strip(), float(UPTIME.read_text().split()[0])


def clock_mark() -> dict[str, object]:
    boot, uptime = boot_clock()
    return {"boot_id": boot, "uptime_seconds": uptime}


def minimum_age(mark: object) -> float:
    # Uptime counts from boot and never steps with the wall clock. A mark of an earlier boot, or
    # one that cannot be read, is at least as old as this boot.
    boot, uptime = boot_clock()
    if isinstance(mark, dict) and mark.get("boot_id") == boot:
        then = mark.get("uptime_seconds")
        if isinstance(then, int | float) and not isinstance(then, bool) and 0 <= then <= uptime:
            return uptime - then
    return uptime


def wait_for_lock(stream: IO[str], wait_seconds: float) -> bool:
    deadline = time.monotonic() + wait_seconds
    while not try_lock(stream):
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)
    return True


@contextmanager
def release_lock(wait_seconds: float) -> Iterator[bool]:
    with open_lock(RELEASE_LOCK) as stream:
        yield wait_for_lock(stream, wait_seconds)
