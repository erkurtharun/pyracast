import uuid
from collections.abc import Callable, Iterator, Sequence
from datetime import timedelta
from types import SimpleNamespace
from typing import ClassVar

import pytest
from django.core.mail import EmailMessage, EmailMultiAlternatives
from django.core.mail.backends.base import BaseEmailBackend
from django.db import OperationalError, connection
from django.test import override_settings

from pyracast.notifications import jobs
from pyracast.notifications.jobs import LEASE_DURATION, process_notification
from pyracast.notifications.mail import NotSentError, build_message
from pyracast.notifications.models import (
    NotificationDelivery,
    NotificationSettings,
    ReminderSchedule,
)
from pyracast.notifications.operations import recipient_limit
from pyracast.notifications.scheduler import MAX_LATENESS
from pyracast.regions.models import Region
from tests.test_notifications import SLOT, ready

pytestmark = pytest.mark.django_db(transaction=True)


class OpeningBackend(BaseEmailBackend):
    action: ClassVar[Callable[[], None] | None] = None
    sent: ClassVar[list[EmailMessage]] = []

    def open(self) -> bool:
        assert not connection.in_atomic_block
        action = type(self).action
        if action is not None:
            action()
        return True

    def send_messages(self, email_messages: Sequence[EmailMessage] | None) -> int:
        assert not connection.in_atomic_block
        assert email_messages is not None
        self.sent.extend(email_messages)
        return len(email_messages)


@pytest.fixture(autouse=True)
def opening_backend() -> Iterator[None]:
    OpeningBackend.action, OpeningBackend.sent = None, []
    with override_settings(
        MAILERS={"default": {"BACKEND": "tests.test_notification_transmission.OpeningBackend"}}
    ):
        yield


def test_reclaimed_connection_cannot_transmit_after_a_new_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery = ready()
    clock = [0.0]
    monkeypatch.setattr(
        "pyracast.notifications.jobs.time", SimpleNamespace(monotonic=lambda: clock[0])
    )

    def reclaim() -> None:
        clock[0] = LEASE_DURATION.total_seconds() + 1
        assert not process_notification(delivery.pk, SLOT + LEASE_DURATION + timedelta(seconds=1))
        delivery.refresh_from_db()
        assert delivery.status == "retry"
        assert delivery.transmit_started_at is None

    OpeningBackend.action = reclaim
    assert not process_notification(delivery.pk, SLOT)
    assert not OpeningBackend.sent
    delivery.refresh_from_db()
    assert delivery.lease_token is None
    OpeningBackend.action = None
    assert process_notification(delivery.pk, SLOT + LEASE_DURATION + timedelta(seconds=2))
    assert len(OpeningBackend.sent) == 1
    delivery.refresh_from_db()
    assert delivery.status == "sent"
    assert delivery.attempts == 2


@pytest.mark.parametrize("failure", [False, True])
def test_late_smtp_response_resolves_the_original_uncertain_lease(
    failure: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery = ready()
    clock = [0.0]
    monkeypatch.setattr(
        "pyracast.notifications.jobs.time", SimpleNamespace(monotonic=lambda: clock[0])
    )

    def late_response(message: EmailMultiAlternatives) -> None:
        delivery.refresh_from_db()
        token = delivery.lease_token
        assert delivery.transmit_started_at is not None
        clock[0] = LEASE_DURATION.total_seconds() + 1
        assert not process_notification(delivery.pk, SLOT + LEASE_DURATION + timedelta(seconds=1))
        delivery.refresh_from_db()
        assert delivery.status == "uncertain"
        assert delivery.lease_token == token
        if failure:
            raise NotSentError("The server explicitly rejected DATA temporarily.")

    assert process_notification(delivery.pk, SLOT, sender=late_response) is not failure
    delivery.refresh_from_db()
    assert delivery.status == ("retry" if failure else "sent")
    assert delivery.lease_token is None
    assert delivery.transmit_started_at is not None


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("authority", "schedule_or_authority_changed"),
        ("settings", "cancelled_by_settings_change"),
        ("limit", "recipient_limit_exceeded"),
        ("deadline", "delivery_deadline_exceeded"),
        ("deleted", ""),
        ("token", ""),
    ],
)
def test_open_connection_does_not_authorize_a_changed_delivery(
    change: str,
    code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery = ready()
    clock = [0.0]
    monkeypatch.setattr(
        "pyracast.notifications.jobs.time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    start = SLOT + MAX_LATENESS - timedelta(seconds=1) if change == "deadline" else SLOT

    def change_delivery() -> None:
        if change == "authority":
            Region.objects.filter(pk=delivery.schedule.region_id).update(status="suspended")
        elif change == "settings":
            delivery.schedule.enabled = False
            delivery.schedule.save(update_fields=("enabled",))
        elif change == "limit":
            NotificationSettings.objects.filter(pk=1).update(max_recipients=1)
            ReminderSchedule.objects.filter(pk=delivery.schedule_id).update(
                recipients=[*delivery.recipients, "other@example.com"]
            )
        elif change == "deadline":
            clock[0] = 2
        elif change == "deleted":
            NotificationDelivery.objects.filter(pk=delivery.pk).delete()
        else:
            NotificationDelivery.objects.filter(pk=delivery.pk).update(lease_token=uuid.uuid4())

    OpeningBackend.action = change_delivery
    assert not process_notification(delivery.pk, start)
    assert not OpeningBackend.sent
    if code:
        delivery.refresh_from_db()
        assert (delivery.status, delivery.last_error) == ("skipped", code)


@pytest.mark.parametrize("exhausted", [False, True])
def test_expired_connection_is_retried_only_within_its_delivery_budget(
    exhausted: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery = ready()
    if exhausted:
        NotificationDelivery.objects.filter(pk=delivery.pk).update(attempts=7)
    clock = [0.0]
    monkeypatch.setattr(
        "pyracast.notifications.jobs.time", SimpleNamespace(monotonic=lambda: clock[0])
    )

    def expire() -> None:
        clock[0] = LEASE_DURATION.total_seconds() + 1

    OpeningBackend.action = expire
    assert not process_notification(delivery.pk, SLOT)
    assert not OpeningBackend.sent
    delivery.refresh_from_db()
    assert delivery.status == ("failed" if exhausted else "retry")
    assert delivery.last_error == "interrupted_connection"


def test_missing_lease_expiry_prevents_transmission() -> None:
    delivery = ready()

    def invalidate_lease() -> None:
        NotificationDelivery.objects.filter(pk=delivery.pk).update(lease_expires_at=None)

    OpeningBackend.action = invalidate_lease
    assert not process_notification(delivery.pk, SLOT)
    assert not OpeningBackend.sent
    delivery.refresh_from_db()
    assert delivery.status == "retry"
    assert delivery.transmit_started_at is None


def test_injected_sender_also_requires_current_transmission_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery = ready()
    clock = [0.0]
    monkeypatch.setattr(
        "pyracast.notifications.jobs.time", SimpleNamespace(monotonic=lambda: clock[0])
    )

    def slow_preparation(record: NotificationDelivery) -> EmailMultiAlternatives:
        clock[0] = LEASE_DURATION.total_seconds() + 1
        return build_message(record)

    monkeypatch.setattr(jobs, "build_message", slow_preparation)
    assert not process_notification(
        delivery.pk, SLOT, sender=lambda message: OpeningBackend.sent.append(message)
    )
    assert not OpeningBackend.sent


def test_mail_authentication_failure_remains_retryable_and_visible() -> None:
    delivery = ready()

    def fail(message: EmailMultiAlternatives) -> None:
        raise NotSentError("Authentication failed.", code="mail_authentication_failed")

    assert not process_notification(delivery.pk, SLOT, sender=fail)
    delivery.refresh_from_db()
    assert delivery.status == "retry"
    assert delivery.last_error == "mail_authentication_failed"


def test_authority_lock_wait_cannot_move_transmission_past_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery = ready()
    clock = [0.0]
    monkeypatch.setattr(
        "pyracast.notifications.jobs.time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    original = recipient_limit
    calls = 0

    def slow_lock(*, lock: bool = False) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            clock[0] = 2
        return original(lock=lock)

    monkeypatch.setattr(jobs, "recipient_limit", slow_lock)
    assert not process_notification(delivery.pk, SLOT + MAX_LATENESS - timedelta(seconds=1))
    assert not OpeningBackend.sent
    delivery.refresh_from_db()
    assert delivery.last_error == "delivery_deadline_exceeded"


def test_database_failure_before_transmission_is_a_known_non_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery = ready()
    original = recipient_limit
    calls = 0

    def unavailable(*, lock: bool = False) -> int:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OperationalError("The database is temporarily unavailable.")
        return original(lock=lock)

    monkeypatch.setattr(jobs, "recipient_limit", unavailable)
    assert not process_notification(delivery.pk, SLOT)
    assert not OpeningBackend.sent
    delivery.refresh_from_db()
    assert delivery.status == "retry"
    assert delivery.last_error == "mail_preparation_failed"
    assert delivery.transmit_started_at is None
