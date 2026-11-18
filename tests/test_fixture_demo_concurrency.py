from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
from time import monotonic

import pytest
from django.contrib.auth.models import User
from django.db import connection, connections, transaction

from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.forecasts.management.commands.fixture_demo import _workspace

pytestmark = pytest.mark.django_db(transaction=True)


def test_concurrent_demo_workspace_creation_resumes_the_same_workspace() -> None:
    actor = User.objects.create_user(username="demo", is_superuser=True, is_staff=True)
    worker_pid: Queue[int] = Queue()
    finished = Event()

    def resume() -> int:
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                worker_pid.put(int(cursor.fetchone()[0]))
            return int(_workspace(actor).pk)
        finally:
            finished.set()
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            first = _workspace(actor)
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                holder_pid = int(cursor.fetchone()[0])
            pending = pool.submit(resume)
            waiter_pid = worker_pid.get(timeout=10)
            deadline = monotonic() + 10
            while True:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT %s = ANY(pg_blocking_pids(%s))", [holder_pid, waiter_pid]
                    )
                    blocked = bool(cursor.fetchone()[0])
                if blocked or finished.wait(timeout=0.01):
                    break
                assert monotonic() < deadline, "The concurrent workspace request did not progress."
        second_id = pending.result(timeout=10)

    assert second_id == first.pk
    assert Tenant.objects.count() == 1
    assert list(Membership.objects.values_list("tenant_id", "user_id", "role")) == [
        (first.pk, actor.pk, WorkspaceRole.ADMIN)
    ]
