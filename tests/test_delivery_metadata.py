from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from pyracast.forecasts.monitoring import delivery_health
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.models import (
    NotificationDelivery,
    NotificationDeliveryStatus,
    PreparedMessageKind,
    ReminderSchedule,
)
from pyracast.platform_admin.health_queries import notification_deliveries_for_issue
from pyracast.platform_admin.read_models import recent_work_context
from tests.support.domain import NOW
from tests.support.notifications import SLOT, configured, ready, reminder_schedule_payload, saved

pytestmark = pytest.mark.django_db(transaction=True)


def trimmed_history() -> tuple[ReminderSchedule, NotificationDelivery, NotificationDelivery]:
    system, schedule = configured()
    snapshot = saved(system, NOW - timedelta(hours=3))

    def sent(hours: int, kind: str, complete: bool | None) -> NotificationDelivery:
        scheduled_for = NOW - timedelta(hours=hours)
        return NotificationDelivery.objects.create(
            schedule=schedule,
            binding=system.binding,
            snapshot=snapshot,
            scheduled_for=scheduled_for,
            preparation_deadline=scheduled_for,
            available_at=scheduled_for,
            status=NotificationDeliveryStatus.SENT,
            sent_at=scheduled_for + timedelta(minutes=30),
            forecast_kind=kind,
            forecast_complete=complete,
            prepared_at=scheduled_for + timedelta(minutes=10),
        )

    partial = sent(1, PreparedMessageKind.SAVED_PARTIAL, False)
    unavailable = sent(2, PreparedMessageKind.UNAVAILABLE, None)
    return schedule, partial, unavailable


def test_monitoring_and_history_read_the_typed_metadata_of_trimmed_deliveries() -> None:
    schedule, partial, unavailable = trimmed_history()

    report = delivery_health(NOW, timedelta(minutes=5))
    history = reminder_schedule_payload(schedule, NOW)["deliveries"]
    console = recent_work_context(now=NOW)["deliveries"]

    assert (
        report["partial_forecasts"],
        report["unavailable_forecasts"],
        report["saved_forecast_notifications"],
    ) == (1, 1, 1)
    assert list(notification_deliveries_for_issue("partial_forecasts", NOW)) == [partial]
    assert list(notification_deliveries_for_issue("unavailable_forecasts", NOW)) == [unavailable]
    assert [
        (row["forecast_status"], row["forecast_complete"], row["source_age_seconds"])
        for row in history
    ] == [
        ("saved_partial", False, timedelta(hours=2, minutes=10).total_seconds()),
        ("unavailable", None, timedelta(hours=1, minutes=10).total_seconds()),
    ]
    assert [row["forecast_status"] for row in console] == [
        "Partial forecast",
        "Forecast unavailable",
    ]


def test_no_monitoring_or_history_query_reads_the_rendered_mail() -> None:
    schedule, _partial, _unavailable = trimmed_history()

    with CaptureQueriesContext(connection) as queries:
        delivery_health(NOW, timedelta(minutes=5))
        list(notification_deliveries_for_issue("partial_forecasts", NOW))
        reminder_schedule_payload(schedule, NOW)
        recent_work_context(now=NOW)

    assert [query["sql"] for query in queries if "prepared_message" in query["sql"]] == []


def test_a_prepared_delivery_keeps_its_metadata_in_columns_and_only_the_mail_in_the_message() -> (
    None
):
    delivery = ready()

    assert process_notification(delivery.pk, SLOT)

    delivery.refresh_from_db()
    assert delivery.forecast_kind in PreparedMessageKind.values
    assert delivery.forecast_complete is not None
    assert delivery.prepared_at is not None
    assert SLOT <= delivery.prepared_at < SLOT + timedelta(minutes=1)
    assert isinstance(delivery.prepared_message, dict)
    assert not {"kind", "tomorrow_complete", "prepared_at"} & delivery.prepared_message.keys()
