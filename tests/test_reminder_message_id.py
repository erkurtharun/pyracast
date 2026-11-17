import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings

from pyracast.notifications.mail import build_message
from pyracast.notifications.models import NotificationDelivery
from tests.test_notifications import SLOT, configured, ready, saved

pytestmark = pytest.mark.django_db(transaction=True)
SENDER = "Pyracast <info@pyracast.app>"


@override_settings(DEFAULT_FROM_EMAIL=SENDER)
def test_message_id_is_unique_per_delivery_and_stable_across_attempts() -> None:
    delivery = ready()
    other = NotificationDelivery.objects.create(
        schedule=delivery.schedule,
        scheduled_for=delivery.scheduled_for,
        available_at=delivery.available_at,
        grouped=False,
        recipients=delivery.recipients,
        binding=delivery.binding,
        snapshot=delivery.snapshot,
    )

    first = build_message(delivery).extra_headers["Message-ID"]

    assert first == f"<reminder.{delivery.message_token}@pyracast.app>"
    assert build_message(delivery).extra_headers["Message-ID"] == first
    assert build_message(other).extra_headers["Message-ID"] != first


def test_existing_deliveries_receive_distinct_message_tokens() -> None:
    system, _schedule = configured()
    saved(system)
    previous = [("notifications", "0009_grouped_reminders")]
    executor = MigrationExecutor(connection)
    current = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate(previous)
        old_apps = executor.loader.project_state(previous).apps
        delivery_model = old_apps.get_model("notifications", "NotificationDelivery")
        schedule_model = old_apps.get_model("notifications", "ReminderSchedule")
        schedule = schedule_model.objects.get()
        for offset, grouped in ((0, True), (1, False), (2, False)):
            delivery_model.objects.create(
                schedule_id=schedule.pk,
                scheduled_for=SLOT.replace(minute=offset),
                available_at=SLOT,
                grouped=grouped,
                status="sent",
            )
    finally:
        MigrationExecutor(connection).migrate(current)

    tokens = list(NotificationDelivery.objects.values_list("message_token", flat=True))
    assert len(tokens) == 3
    assert None not in tokens
    assert len(set(tokens)) == 3
