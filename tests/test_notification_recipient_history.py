from collections.abc import Iterator
from datetime import timedelta

import pytest
from allauth.account.models import EmailAddress
from django.core import mail
from django.test import override_settings

from pyracast.accounts.models import Membership
from pyracast.accounts.operations import remove_workspace_member
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.models import NotificationDelivery, ReminderSchedule
from pyracast.notifications.recipients import effective_recipients
from pyracast.notifications.scheduler import enqueue_due
from pyracast.regions.operations import submit_region
from tests.test_domain import NOW
from tests.test_notification_groups import group, member
from tests.test_notifications import SLOT, configured

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def memory_mail() -> Iterator[None]:
    with override_settings(
        MAILERS={"default": {"BACKEND": "django.core.mail.backends.locmem.EmailBackend"}}
    ):
        yield


def test_sent_history_revokes_an_address_that_another_delivery_pinned_as_external() -> None:
    system, _ = configured()
    schedule = group(system, ["old@example.com", "retained@example.com"])
    pending = NotificationDelivery.objects.get(pk=enqueue_due(SLOT)[0])
    assert pending.recipient_memberships == {}
    user = member(system, "old@example.com")
    membership = Membership.objects.get(tenant=system.tenant, user=user)
    historical = NotificationDelivery.objects.create(
        schedule=schedule,
        scheduled_for=SLOT + timedelta(minutes=1),
        available_at=SLOT,
        status="sent",
        recipients=[user.email],
        recipient_memberships={user.email: [membership.pk]},
        sent_at=SLOT + timedelta(minutes=1),
    )
    user.email = "new@example.com"
    user.save(update_fields=["email"])
    EmailAddress.objects.filter(user=user).delete()
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    remove_workspace_member(system.member, system.tenant.pk, user.pk)
    pending.refresh_from_db()
    historical.refresh_from_db()
    schedule.refresh_from_db()
    assert pending.revoked_recipients == ["old@example.com"]
    assert pending.recipients == ["old@example.com", "retained@example.com"]
    assert schedule.recipients == ["retained@example.com"]
    assert historical.status == "sent"
    assert historical.recipients == ["old@example.com"]
    assert historical.revoked_recipients == []

    Membership.objects.create(tenant=system.tenant, user=user)
    user.email = "old@example.com"
    user.save(update_fields=["email"])
    EmailAddress.objects.create(user=user, email=user.email, verified=True)
    assert set(effective_recipients(schedule)) == {"old@example.com", "retained@example.com"}
    assert process_notification(pending.pk, SLOT)
    assert mail.outbox[0].bcc == ["retained@example.com"]


def test_later_schedule_alias_evidence_prunes_earlier_external_schedule() -> None:
    system, _ = configured()
    first = group(system, ["OLD@example.com", "retained@example.com"])
    assert first.recipient_memberships == {}
    user = member(system, "old@example.com")
    membership = Membership.objects.get(tenant=system.tenant, user=user)
    region = submit_region(
        system.member,
        system.tenant.pk,
        name="Alias evidence",
        latitude=41,
        longitude=30,
        timezone="Europe/Istanbul",
    )
    evidence = ReminderSchedule.objects.create(
        region=region,
        effective_from=NOW,
        recipients=[user.email],
        recipient_memberships={user.email: [membership.pk]},
    )
    user.email = "new@example.com"
    user.save(update_fields=["email"])
    EmailAddress.objects.filter(user=user).delete()
    EmailAddress.objects.create(user=user, email=user.email, verified=True)
    remove_workspace_member(system.member, system.tenant.pk, user.pk)
    first.refresh_from_db()
    evidence.refresh_from_db()
    assert first.recipients == ["retained@example.com"]
    assert effective_recipients(first) == ["retained@example.com"]
    assert evidence.recipients == []
    assert evidence.recipient_memberships == {}


def test_historical_alias_does_not_revoke_a_different_pinned_member() -> None:
    system, _ = configured()
    former = member(system, "former@example.com")
    former_membership = Membership.objects.get(tenant=system.tenant, user=former)
    current = member(system, "shared@example.com")
    current_membership = Membership.objects.get(tenant=system.tenant, user=current)
    schedule = group(system, [current.email])
    pending = NotificationDelivery.objects.get(pk=enqueue_due(SLOT)[0])
    historical = NotificationDelivery.objects.create(
        schedule=schedule,
        scheduled_for=SLOT - timedelta(days=1),
        available_at=SLOT - timedelta(days=1),
        status="sent",
        recipients=[current.email],
        recipient_memberships={current.email: [former_membership.pk]},
    )
    remove_workspace_member(system.member, system.tenant.pk, former.pk)
    schedule.refresh_from_db()
    pending.refresh_from_db()
    historical.refresh_from_db()
    assert schedule.recipients == [current.email]
    assert schedule.recipient_memberships == {current.email: [current_membership.pk]}
    assert pending.recipient_memberships[current.email] == [current_membership.pk]
    assert pending.revoked_recipients == [former.email]
    assert historical.recipients == [current.email]
    assert historical.revoked_recipients == []
    assert process_notification(pending.pk, SLOT)
    assert mail.outbox[0].bcc == [current.email]
