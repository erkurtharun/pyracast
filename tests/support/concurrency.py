import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import pytest
from django.db import connection, connections, transaction
from django.db.models import Model


def _waits_for(holder_pid: int, waiter_pid: int) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT %s = ANY(pg_blocking_pids(%s))", [holder_pid, waiter_pid])
        row = cursor.fetchone()
    return bool(row and row[0])


def behind_open_transaction[T, U](holder: Callable[[], T], waiter: Callable[[], U]) -> tuple[T, U]:
    # The holder commits only once the waiter blocks on one of its locks (or has finished), so
    # whatever the waiter reads before it blocks comes from the state before the holder's change.
    held, release = Event(), Event()
    holder_pid: list[int] = []
    waiter_pid: list[int] = []
    waiter_ready = Event()

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
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                waiter_pid.extend(cursor.fetchone() or ())
            waiter_ready.set()
            return waiter()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(2) as pool:
        holding = pool.submit(hold)
        try:
            assert held.wait(timeout=10), holding.exception(timeout=0)
            waiting = pool.submit(wait)
            assert waiter_ready.wait(timeout=10)
            deadline = time.monotonic() + 10
            while not waiting.done() and not _waits_for(holder_pid[0], waiter_pid[0]):
                assert time.monotonic() < deadline
                time.sleep(0.02)
        finally:
            release.set()
        return holding.result(timeout=20), waiting.result(timeout=20)


def _backend_pid() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        row = cursor.fetchone()
    assert row is not None
    return int(row[0])


def paused_in[T, U](
    monkeypatch: pytest.MonkeyPatch,
    owner: object,
    step: str,
    holder: Callable[[], T],
    other: Callable[[], U],
) -> tuple[T, U]:
    # The holder pauses when it calls `owner.step`, keeping every lock it took before; it goes on
    # once the other client waits for one of those locks or has finished.
    original = getattr(owner, step)
    paused, resume = Event(), Event()
    pids: dict[str, int] = {}

    def pause(*args: object, **kwargs: object) -> object:
        paused.set()
        assert resume.wait(timeout=20)
        return original(*args, **kwargs)

    def run[R](name: str, action: Callable[[], R]) -> R:
        try:
            pids[name] = _backend_pid()
            return action()
        finally:
            connections.close_all()

    monkeypatch.setattr(owner, step, pause)
    with ThreadPoolExecutor(2) as pool:
        holding = pool.submit(run, "holder", holder)
        try:
            assert paused.wait(timeout=10)
            reaching = pool.submit(run, "other", other)
            deadline = time.monotonic() + 10
            while not reaching.done() and not (
                "other" in pids and _waits_for(pids["holder"], pids["other"])
            ):
                assert time.monotonic() < deadline
                time.sleep(0.02)
        finally:
            resume.set()
        return holding.result(timeout=20), reaching.result(timeout=20)


def committed_elsewhere[T](action: Callable[[], T]) -> T:
    # Runs the action in its own transaction on another connection and returns once it commits.
    def run() -> T:
        try:
            with transaction.atomic():
                return action()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(run).result(timeout=20)


@contextmanager
def short_lock_timeout() -> Iterator[None]:
    with connection.cursor() as cursor:
        cursor.execute("SET lock_timeout = '300ms'")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET lock_timeout")


@contextmanager
def row_locked_elsewhere(model: type[Model], pk: int) -> Iterator[None]:
    held, release = Event(), Event()

    def hold() -> None:
        try:
            with transaction.atomic():
                model._default_manager.select_for_update().get(pk=pk)
                held.set()
                assert release.wait(timeout=30)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(hold)
        try:
            assert held.wait(timeout=10)
            yield
        finally:
            release.set()
            holder.result(timeout=10)
