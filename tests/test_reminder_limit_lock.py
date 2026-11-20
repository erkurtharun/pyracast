from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from threading import Event

import pytest
from django.db import close_old_connections, connection, connections

from pyracast.accounts.models import Tenant
from pyracast.notifications import jobs
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.models import NotificationDelivery, NotificationDeliveryStatus
from pyracast.notifications.operations import configure_reminders
from pyracast.notifications.preparation import prepare_delivery
from pyracast.notifications.scheduler import enqueue_due
from tests.support.budgets import captured_statements
from tests.support.domain import NOW
from tests.support.notification_groups import second_workspace
from tests.support.notifications import SLOT, configured, saved
from tests.support.trash_boundaries import _race_before_region_lock

pytestmark = pytest.mark.django_db(transaction=True, databases=["default", "auth_cache"])


@contextmanager
def short_lock_wait() -> Iterator[None]:
    with connection.cursor() as cursor:
        cursor.execute("SET lock_timeout = '500ms'")
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET lock_timeout")


def test_a_delivery_in_preparation_holds_back_no_other_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _ = configured()
    saved(system)
    other = second_workspace(system)
    (delivery_id,) = enqueue_due(SLOT)
    preparing, release = Event(), Event()

    def slow_preparation(delivery: NotificationDelivery, now: datetime) -> bool:
        preparing.set()
        assert release.wait(10)
        return prepare_delivery(delivery, now)

    monkeypatch.setattr(jobs, "prepare_delivery", slow_preparation)

    def deliver() -> bool:
        close_old_connections()
        try:
            return process_notification(delivery_id, SLOT)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(1) as pool:
        delivering = pool.submit(deliver)
        try:
            assert preparing.wait(10)
            with short_lock_wait():
                configure_reminders(
                    other.member,
                    other.tenant.pk,
                    other.region.pk,
                    enabled=True,
                    recipients=["second@example.com"],
                    times=["10:00"],
                    language="en",
                    now=NOW,
                )
                scheduled = enqueue_due(SLOT)
        finally:
            release.set()
        assert delivering.result(timeout=20)

    assert len(scheduled) == 2
    assert NotificationDelivery.objects.get(schedule__region=other.region).status == (
        NotificationDeliveryStatus.PENDING
    )


def test_a_slot_another_pass_creates_while_this_one_waits_for_its_locks_is_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured()
    created: list[list[int]] = []

    waited = _race_before_region_lock(
        monkeypatch,
        "get",
        lambda: enqueue_due(SLOT),
        lambda: created.append(enqueue_due(SLOT)),
        lock_model=Tenant,
    )

    assert waited == created[0]
    assert NotificationDelivery.objects.count() == 1


def test_a_scheduler_pass_over_slots_that_exist_takes_no_lock() -> None:
    configured()
    first = enqueue_due(SLOT)

    with captured_statements() as statements:
        again = enqueue_due(SLOT)

    assert again == first
    assert [statement for statement in statements if " FOR " in statement] == []
