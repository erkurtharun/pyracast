from collections.abc import Iterator
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.core.mail import EmailMultiAlternatives
from django.db import OperationalError
from django.test import override_settings

from pyracast.notifications import jobs
from pyracast.notifications.mail import NotSentError
from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.operations import recipient_limit
from pyracast.notifications.preparation import prepare_delivery
from pyracast.notifications.scheduler import enqueue_due
from tests.test_notifications import SLOT, configured, ready, saved

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def memory_mail() -> Iterator[None]:
    with override_settings(
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
    ):
        yield


@pytest.mark.parametrize(
    "renderer",
    [
        "pyracast.notifications.jobs.build_message",
        "pyracast.notifications.preparation.build_message",
    ],
)
def test_unexpected_render_failure_sends_a_frozen_plain_unavailable_notice(
    renderer: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery = ready()

    def broken(*args: object, **kwargs: object) -> EmailMultiAlternatives:
        raise RuntimeError("The shared renderer cannot be used.")

    monkeypatch.setattr(renderer, broken)
    messages: list[EmailMultiAlternatives] = []

    def first(message: EmailMultiAlternatives) -> None:
        messages.append(message)
        raise NotSentError

    assert not jobs.process_notification(delivery.pk, SLOT, sender=first)
    delivery.refresh_from_db()
    prepared = delivery.prepared_message
    assert prepared is not None
    assert delivery.status == "retry"
    assert prepared["kind"] == "unavailable"
    assert prepared["format"] == "plain"
    assert delivery.snapshot_id is None
    assert jobs.process_notification(delivery.pk, delivery.available_at, sender=messages.append)
    delivery.refresh_from_db()
    assert delivery.prepared_message == prepared
    assert delivery.status == "sent"
    assert len(messages) == 2
    assert messages[0].body == messages[1].body
    assert "No forecast energy total is available" in messages[0].body
    assert not messages[0].attachments


def test_database_failure_during_preparation_remains_durable_unsent_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery = ready()

    def unavailable(record: NotificationDelivery, now: object) -> bool:
        raise OperationalError("The database cannot prepare the message.")

    monkeypatch.setattr(jobs, "prepare_delivery", unavailable)
    with pytest.raises(OperationalError):
        jobs.process_notification(delivery.pk, SLOT)
    delivery.refresh_from_db()
    assert delivery.status == "pending"
    assert delivery.attempts == 0
    assert delivery.prepared_message is None


def test_render_failure_on_retry_keeps_the_prepared_forecast_vintage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery = ready()
    assert prepare_delivery(delivery, SLOT)
    delivery.save()
    prepared = delivery.prepared_message
    assert isinstance(prepared, dict)
    assert delivery.snapshot_id is not None

    def broken(*args: object, **kwargs: object) -> EmailMultiAlternatives:
        raise RuntimeError("Rendering became unavailable after the slot was prepared.")

    monkeypatch.setattr(jobs, "build_message", broken)
    assert jobs.process_notification(delivery.pk, SLOT)
    stored = NotificationDelivery.objects.get(pk=delivery.pk)
    assert stored.status == "sent"
    assert stored.snapshot_id == delivery.snapshot_id
    assert isinstance(stored.prepared_message, dict)
    assert stored.prepared_message["format"] == "plain"
    assert stored.prepared_message["prepared_at"] == prepared["prepared_at"]


def test_forecast_published_during_claim_lock_wait_is_available_to_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, _ = configured()
    older = saved(system)
    newer = saved(system, SLOT + timedelta(minutes=1))
    newer.job.scheduled_slot = SLOT
    newer.job.save(update_fields=("scheduled_slot",))
    delivery_id = enqueue_due(SLOT)[0]
    clock = [0.0]
    monkeypatch.setattr(jobs, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def wait_for_lock(*, lock: bool = False) -> int:
        clock[0] = 120
        return recipient_limit(lock=lock)

    monkeypatch.setattr(jobs, "recipient_limit", wait_for_lock)
    assert jobs.process_notification(delivery_id, SLOT)
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    assert delivery.snapshot_id == newer.pk != older.pk
