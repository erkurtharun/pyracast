import uuid
from datetime import timedelta

import pytest
from pyracast.orchestration.delivery_health import CHECKS, Window, check, delivery_health, listed

from pyracast.forecasts.jobs.ready import pending_job_ids
from pyracast.forecasts.models import CollectionJob
from pyracast.notifications.jobs import pending_notification_ids
from pyracast.notifications.models import NotificationDelivery, ReminderSchedule
from pyracast.notifications.scheduler import PREPARATION_GRACE
from pyracast.platform_admin.health import PRESENTATION
from tests.support.delivery_health import NOW
from tests.support.domain import make_system

LAG = timedelta(minutes=5)


def test_the_console_presents_every_check_that_can_turn_the_report_unhealthy() -> None:
    keys = {item.key for item in CHECKS}

    assert len(keys) == len(CHECKS)
    assert {item.key for item in CHECKS if item.alarm} <= set(PRESENTATION) <= keys


def test_an_unknown_check_is_refused() -> None:
    with pytest.raises(KeyError):
        check("not_a_check")


@pytest.mark.django_db(transaction=True)
def test_overdue_and_interrupted_jobs_are_jobs_their_owners_publish() -> None:
    system = make_system(approved=True)
    late = NOW - LAG - timedelta(seconds=1)
    CollectionJob.objects.filter(binding=system.binding).update(available_at=late)
    CollectionJob.objects.create(
        binding=system.binding,
        target_date=NOW.date(),
        scheduled_for=NOW - timedelta(hours=1),
        available_at=late,
        status="running",
        lease_token=uuid.uuid4(),
        lease_expires_at=NOW,
    )
    schedule = ReminderSchedule.objects.create(region=system.region, effective_from=NOW)
    for index, (status, lease) in enumerate((("pending", None), ("sending", NOW))):
        NotificationDelivery.objects.create(
            schedule=schedule,
            binding=system.binding,
            recipients=["recipient@example.com"],
            scheduled_for=late - timedelta(minutes=index),
            preparation_deadline=late + PREPARATION_GRACE,
            available_at=late,
            status=status,
            lease_token=None if lease is None else uuid.uuid4(),
            lease_expires_at=lease,
            prepared_message={"kind": "current", "tomorrow_complete": True},
        )
    window = Window(NOW, LAG)
    report = delivery_health(NOW, LAG)

    for key, ready in (
        ("overdue_collections", pending_job_ids(NOW)),
        ("interrupted_collections", pending_job_ids(NOW)),
        ("overdue_notifications", pending_notification_ids(NOW)),
        ("interrupted_notifications", pending_notification_ids(NOW)),
    ):
        rows = set(listed(check(key), window).values_list("pk", flat=True))
        assert len(rows) == report.counts[key] == 1, key
        assert rows <= set(ready), key
