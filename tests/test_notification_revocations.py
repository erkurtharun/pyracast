from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.core import mail
from django.core.mail import EmailMultiAlternatives
from django.test import override_settings

from pyracast.accounts.models import Membership
from pyracast.accounts.operations import remove_workspace_member
from pyracast.notifications import jobs
from pyracast.notifications.jobs import LEASE_DURATION, process_notification
from pyracast.notifications.mail import NotSentError
from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.scheduler import enqueue_due
from tests.test_notification_groups import group, member
from tests.test_notification_transmission import OpeningBackend
from tests.test_notifications import SLOT, configured
from tests.test_notifications import memory_mail as memory_mail

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("memory_mail")]


def test_external_recipient_removed_after_join_cannot_rejoin_an_old_pending_group() -> None:
    system, _ = configured()
    retained = member(system, "retained@example.com")
    group(system, ["EXTERNAL@example.com"])
    (delivery_id,) = enqueue_due(SLOT)
    original = NotificationDelivery.objects.get(pk=delivery_id)
    assert "external@example.com" not in original.recipient_memberships
    external = member(system, "external@example.com")
    removed_membership = Membership.objects.get(user=external)
    remove_workspace_member(system.member, system.tenant.pk, external.pk)
    replacement = Membership.objects.create(tenant=system.tenant, user=external)
    assert replacement.pk != removed_membership.pk
    assert process_notification(delivery_id, SLOT)
    original.refresh_from_db()
    assert original.revoked_recipients == [external.email]
    assert original.recipients == [retained.email]
    assert mail.outbox[0].bcc == [retained.email]
    next_slot = SLOT + timedelta(days=1)
    next_id = enqueue_due(next_slot)[-1]
    next_delivery = NotificationDelivery.objects.get(pk=next_id)
    assert set(next_delivery.recipients) == {retained.email, external.email}
    assert next_delivery.revoked_recipients == []
    assert process_notification(next_id, next_slot)
    assert set(mail.outbox[1].bcc) == {retained.email, external.email}


def test_known_non_acceptance_marker_does_not_exempt_a_retry_from_member_revocation() -> None:
    system, _ = configured()
    retained = member(system, "retained@example.com")
    group(system, ["EXTERNAL@example.com"])
    (delivery_id,) = enqueue_due(SLOT)
    attempted: list[list[str]] = []

    def rejected(message: EmailMultiAlternatives) -> None:
        attempted.append(list(message.bcc))
        raise NotSentError

    assert not process_notification(delivery_id, SLOT, sender=rejected)
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    frozen = delivery.prepared_message
    assert delivery.status == "retry"
    assert delivery.transmit_started_at is not None
    external = member(system, "external@example.com")
    remove_workspace_member(system.member, system.tenant.pk, external.pk)
    Membership.objects.create(tenant=system.tenant, user=external)
    assert process_notification(delivery_id, delivery.available_at)
    delivery.refresh_from_db()
    assert delivery.revoked_recipients == [external.email]
    assert delivery.recipients == [retained.email]
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
            MAILERS={"default": {"BACKEND": "tests.test_notification_transmission.OpeningBackend"}}
        ):
            assert process_notification(delivery_id, SLOT)
    finally:
        OpeningBackend.action = None
    assert len(OpeningBackend.sent) == 1
    assert OpeningBackend.sent[0].bcc == [retained.email]
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    assert delivery.revoked_recipients == ["external@example.com"]
    assert delivery.recipients == [retained.email]


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
    attempted: list[EmailMultiAlternatives] = []
    original_recipients = ["EXTERNAL@example.com", retained.email]

    def rejected_after_revocation(message: EmailMultiAlternatives) -> None:
        attempted.append(message)
        delivery = NotificationDelivery.objects.get(pk=delivery_id)
        assert delivery.status == "sending"
        assert delivery.transmit_started_at is not None
        assert message.bcc == delivery.recipients == original_recipients
        if recover_interrupted:
            clock[0] = LEASE_DURATION.total_seconds() + 1
            assert not process_notification(delivery_id, SLOT + timedelta(seconds=clock[0]))
            delivery.refresh_from_db()
            assert delivery.status == "uncertain"
        external = member(system, "external@example.com")
        remove_workspace_member(system.member, system.tenant.pk, external.pk)
        Membership.objects.create(tenant=system.tenant, user=external)
        delivery.refresh_from_db()
        assert delivery.revoked_recipients == [external.email]
        assert delivery.recipients == original_recipients
        assert message.bcc == original_recipients
        raise NotSentError

    assert not process_notification(delivery_id, SLOT, sender=rejected_after_revocation)
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    frozen = delivery.prepared_message
    assert delivery.status == "retry"
    assert delivery.recipients == original_recipients
    assert delivery.revoked_recipients == ["external@example.com"]
    assert process_notification(delivery_id, delivery.available_at)
    delivery.refresh_from_db()
    assert delivery.status == "sent"
    assert delivery.attempts == 2
    assert delivery.recipients == [retained.email]
    assert delivery.prepared_message == frozen
    assert attempted[0].bcc == original_recipients
    assert mail.outbox[0].bcc == [retained.email]
