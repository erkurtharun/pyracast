from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.core import mail
from django.core.mail import EmailMessage
from django.test import override_settings

from pyracast.accounts.models import Membership
from pyracast.accounts.operations import remove_workspace_member
from pyracast.mail.errors import NotSentError
from pyracast.notifications import jobs
from pyracast.notifications.jobs import LEASE_DURATION, process_notification
from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.scheduler import enqueue_due
from tests.support.notification_groups import group, member
from tests.support.notification_transmission import OpeningBackend, replying
from tests.support.notifications import (
    SLOT,
    candidates,
    configured,
    reminder_schedule_payload,
    revoked,
    ties,
)

pytestmark = pytest.mark.django_db(transaction=True)


def test_external_recipient_removed_after_join_cannot_rejoin_an_old_pending_group() -> None:
    system, _ = configured()
    retained = member(system, "retained@example.com")
    group(system, ["EXTERNAL@example.com"])
    (delivery_id,) = enqueue_due(SLOT)
    original = NotificationDelivery.objects.get(pk=delivery_id)
    assert "external@example.com" not in ties(original.schedule)
    external = member(system, "external@example.com")
    removed_membership = Membership.objects.get(user=external)
    remove_workspace_member(system.member, system.tenant.pk, external.pk)
    replacement = Membership.objects.create(tenant=system.tenant, user=external)
    assert replacement.pk != removed_membership.pk
    assert process_notification(delivery_id, SLOT)
    original.refresh_from_db()
    assert (candidates(original), revoked(original)) == ([retained.email], [])
    assert mail.outbox[0].bcc == [retained.email]
    next_slot = SLOT + timedelta(days=1)
    next_id = enqueue_due(next_slot)[-1]
    next_delivery = NotificationDelivery.objects.get(pk=next_id)
    assert set(candidates(next_delivery)) == {retained.email, external.email}
    assert revoked(next_delivery) == []
    assert process_notification(next_id, next_slot)
    assert set(mail.outbox[1].bcc) == {retained.email, external.email}


def test_known_non_acceptance_marker_does_not_exempt_a_retry_from_member_revocation() -> None:
    system, _ = configured()
    retained = member(system, "retained@example.com")
    group(system, ["EXTERNAL@example.com"])
    (delivery_id,) = enqueue_due(SLOT)
    attempted: list[list[str]] = []

    def rejected(message: EmailMessage) -> None:
        attempted.append(list(message.bcc))
        raise NotSentError

    with replying(rejected):
        assert not process_notification(delivery_id, SLOT)
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    frozen = delivery.prepared_message
    assert delivery.status == "retry"
    assert delivery.transmit_started_at is not None
    external = member(system, "external@example.com")
    remove_workspace_member(system.member, system.tenant.pk, external.pk)
    Membership.objects.create(tenant=system.tenant, user=external)
    assert process_notification(delivery_id, delivery.available_at)
    delivery.refresh_from_db()
    assert (candidates(delivery), revoked(delivery)) == ([retained.email], [])
    assert delivery.prepared_message == frozen
    assert delivery.attempts == 2
    assert attempted == [["EXTERNAL@example.com", retained.email]]
    assert mail.outbox[0].bcc == [retained.email]


def test_join_remove_and_rejoin_during_connection_open_cannot_restore_the_envelope() -> None:
    system, _ = configured()
    retained = member(system, "retained@example.com")
    group(system, ["EXTERNAL@example.com"])
    (delivery_id,) = enqueue_due(SLOT)

    def change_membership() -> None:
        external = member(system, "external@example.com")
        remove_workspace_member(system.member, system.tenant.pk, external.pk)
        Membership.objects.create(tenant=system.tenant, user=external)

    OpeningBackend.sent = []
    OpeningBackend.action = change_membership
    try:
        with override_settings(
            MAILERS={
                "default": {"BACKEND": "tests.support.notification_transmission.OpeningBackend"}
            }
        ):
            assert process_notification(delivery_id, SLOT)
    finally:
        OpeningBackend.action = None
    assert len(OpeningBackend.sent) == 1
    assert OpeningBackend.sent[0].bcc == [retained.email]
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    assert (candidates(delivery), revoked(delivery)) == ([retained.email], [])


def test_a_member_removed_after_authorization_stays_in_the_history_of_the_sent_delivery() -> None:
    system, _ = configured()
    member(system, "retained@example.com")
    leaving = member(system, "leaving@example.com")
    schedule = group(system)
    (delivery_id,) = enqueue_due(SLOT)
    envelopes: list[list[str]] = []

    def accepted_after_removal(message: EmailMessage) -> None:
        envelopes.append(sorted(message.bcc))
        remove_workspace_member(system.member, system.tenant.pk, leaving.pk)

    with replying(accepted_after_removal):
        assert process_notification(delivery_id, SLOT)

    administrator, viewer = (
        reminder_schedule_payload(schedule, SLOT, administrator=flag)["deliveries"][0]
        for flag in (True, False)
    )
    assert envelopes == [["leaving@example.com", "retained@example.com"]]
    assert (administrator["status"], sorted(administrator["recipients"])) == (
        "sent",
        envelopes[0],
    )
    assert (administrator["recipient_count"], viewer["recipient_count"]) == (2, 2)
    assert viewer["recipients"] is None


@pytest.mark.parametrize("recover_interrupted", [False, True])
def test_revocation_after_permission_preserves_the_attempt_but_excludes_safe_retries(
    recover_interrupted: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    system, _ = configured()
    retained = member(system, "retained@example.com")
    group(system, ["EXTERNAL@example.com"])
    (delivery_id,) = enqueue_due(SLOT)
    clock = [0.0]
    monkeypatch.setattr(jobs, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    attempted: list[EmailMessage] = []
    original_recipients = ["EXTERNAL@example.com", retained.email]

    def rejected_after_revocation(message: EmailMessage) -> None:
        attempted.append(message)
        delivery = NotificationDelivery.objects.get(pk=delivery_id)
        assert delivery.status == "sending"
        assert delivery.transmit_started_at is not None
        assert message.bcc == candidates(delivery) == original_recipients
        if recover_interrupted:
            clock[0] = LEASE_DURATION.total_seconds() + 1
            assert not process_notification(delivery_id, SLOT + timedelta(seconds=clock[0]))
            delivery.refresh_from_db()
            assert delivery.status == "uncertain"
        external = member(system, "external@example.com")
        remove_workspace_member(system.member, system.tenant.pk, external.pk)
        Membership.objects.create(tenant=system.tenant, user=external)
        delivery.refresh_from_db()
        assert revoked(delivery) == [external.email]
        assert candidates(delivery) == original_recipients
        assert message.bcc == original_recipients
        raise NotSentError

    with replying(rejected_after_revocation):
        assert not process_notification(delivery_id, SLOT)
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    frozen = delivery.prepared_message
    assert delivery.status == "retry"
    assert candidates(delivery) == original_recipients
    assert revoked(delivery) == ["external@example.com"]
    assert process_notification(delivery_id, delivery.available_at)
    delivery.refresh_from_db()
    assert delivery.status == "sent"
    assert delivery.attempts == 2
    assert candidates(delivery) == [retained.email]
    assert delivery.prepared_message == frozen
    assert attempted[0].bcc == original_recipients
    assert mail.outbox[0].bcc == [retained.email]
