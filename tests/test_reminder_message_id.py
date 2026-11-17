import pytest
from django.db import IntegrityError, transaction
from django.test import override_settings

from pyracast.notifications.mail import build_message
from pyracast.notifications.models import NotificationDelivery
from tests.support.notifications import SLOT, configured, ready

pytestmark = pytest.mark.django_db(transaction=True)
SENDER = "Pyracast <info@pyracast.app>"


@override_settings(DEFAULT_FROM_EMAIL=SENDER)
def test_message_id_is_unique_per_delivery_and_stable_across_attempts() -> None:
    delivery = ready()
    other = NotificationDelivery.objects.create(
        schedule=delivery.schedule,
        scheduled_for=delivery.scheduled_for.replace(minute=1),
        available_at=delivery.available_at,
        recipients=delivery.recipients,
        binding=delivery.binding,
        snapshot=delivery.snapshot,
    )

    first = build_message(delivery).extra_headers["Message-ID"]

    assert first == f"<reminder.{delivery.message_token}@pyracast.app>"
    assert build_message(delivery).extra_headers["Message-ID"] == first
    assert build_message(other).extra_headers["Message-ID"] != first


def test_every_delivery_holds_its_own_message_token() -> None:
    _system, schedule = configured()
    deliveries = [
        NotificationDelivery.objects.create(
            schedule=schedule,
            scheduled_for=SLOT.replace(minute=offset),
            available_at=SLOT,
            status="sent",
        )
        for offset in range(3)
    ]

    tokens = list(NotificationDelivery.objects.values_list("message_token", flat=True))
    assert len(tokens) == 3
    assert None not in tokens
    assert len(set(tokens)) == 3
    with pytest.raises(IntegrityError), transaction.atomic():
        NotificationDelivery.objects.filter(pk=deliveries[1].pk).update(
            message_token=deliveries[0].message_token
        )
