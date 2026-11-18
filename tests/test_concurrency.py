import time
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event

import pytest
from django.db import connection, connections, transaction

from tests.support.concurrency import _waits_for

pytestmark = pytest.mark.django_db(transaction=True)


def test_lock_observation_identifies_the_waiter_instead_of_an_unrelated_session() -> None:
    held, release = Event(), Event()
    holder_pid: Queue[int] = Queue()
    waiter_pid: Queue[int] = Queue()

    def hold() -> None:
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid(), pg_advisory_xact_lock(46301)")
                holder_pid.put(cursor.fetchone()[0])
                held.set()
                assert release.wait(timeout=10)
        finally:
            connections.close_all()

    def wait() -> None:
        try:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                waiter_pid.put(cursor.fetchone()[0])
                cursor.execute("SELECT pg_advisory_xact_lock(46301)")
        finally:
            connections.close_all()

    with ThreadPoolExecutor(2) as pool:
        holding = pool.submit(hold)
        try:
            assert held.wait(timeout=10)
            holder = holder_pid.get(timeout=5)
            waiting = pool.submit(wait)
            waiter = waiter_pid.get(timeout=5)
            deadline = time.monotonic() + 5
            with connection.cursor() as cursor:
                while True:
                    cursor.execute("SELECT %s = ANY(pg_blocking_pids(%s))", [holder, waiter])
                    if cursor.fetchone()[0]:
                        break
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                cursor.execute("SELECT pg_backend_pid()")
                unrelated = cursor.fetchone()[0]
            assert _waits_for(holder, waiter)
            assert not _waits_for(holder, unrelated)
        finally:
            release.set()
        holding.result(timeout=10)
        waiting.result(timeout=10)
