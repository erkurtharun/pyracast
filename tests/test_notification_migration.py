from datetime import timedelta

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from pyracast.notifications.jobs import process_notification
from pyracast.notifications.models import NotificationDelivery
from tests.test_notifications import SLOT, ready

pytestmark = pytest.mark.django_db(transaction=True)


def test_upgrade_preserves_legacy_smtp_ambiguity_without_marking_unstarted_work() -> None:
    delivery = ready()
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    previous = [("notifications", "0005_remove_redundant_reminder_email")]
    executor.migrate(previous)
    try:
        records = executor.loader.project_state(previous).apps.get_model(
            "notifications", "NotificationDelivery"
        )
        keys: dict[str, int] = {}
        for offset, status in enumerate(("sending", "uncertain", "pending", "retry"), start=1):
            record = records.objects.create(
                preference_id=delivery.preference_id,
                recipient=delivery.recipient,
                snapshot_id=delivery.snapshot_id,
                scheduled_for=SLOT + timedelta(seconds=offset),
                available_at=SLOT,
                status=status,
                lease_expires_at=SLOT if status == "sending" else None,
            )
            keys[status] = record.pk
    finally:
        MigrationExecutor(connection).migrate(latest)
    for status, key in keys.items():
        current = NotificationDelivery.objects.get(pk=key)
        assert current.transmit_started_at == (
            current.scheduled_for if status in {"sending", "uncertain"} else None
        )
    assert not process_notification(keys["sending"], SLOT + timedelta(minutes=10))
    assert NotificationDelivery.objects.get(pk=keys["sending"]).status == "uncertain"
