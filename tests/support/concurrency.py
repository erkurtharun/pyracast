import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from django.db import connection, connections, transaction


def _waits_for(pid: int) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE %s = ANY(pg_blocking_pids(pid))", [pid]
        )
        row = cursor.fetchone()
    return bool(row and row[0])


def behind_open_transaction[T, U](holder: Callable[[], T], waiter: Callable[[], U]) -> tuple[T, U]:
    # The holder commits only once the waiter blocks on one of its locks (or has finished), so
    # whatever the waiter reads before it blocks comes from the state before the holder's change.
    held, release = Event(), Event()
    holder_pid: list[int] = []

    def hold() -> T:
        try:
            with transaction.atomic():
                result = holder()
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    holder_pid.extend(cursor.fetchone() or ())
                held.set()
                assert release.wait(timeout=10)
            return result
        finally:
            connections.close_all()

    def wait() -> U:
        try:
            return waiter()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(2) as pool:
        holding = pool.submit(hold)
        try:
            assert held.wait(timeout=10), holding.exception(timeout=0)
            waiting = pool.submit(wait)
            deadline = time.monotonic() + 10
            while not waiting.done() and not _waits_for(holder_pid[0]):
                assert time.monotonic() < deadline
                time.sleep(0.02)
        finally:
            release.set()
        return holding.result(timeout=20), waiting.result(timeout=20)
