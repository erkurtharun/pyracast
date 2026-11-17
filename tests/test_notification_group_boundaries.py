from datetime import timedelta

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.core import mail

from pyracast.accounts.models import Membership
from pyracast.accounts.operations import remove_workspace_member
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.operations import configure_reminders
from pyracast.notifications.scheduler import enqueue_due
from pyracast.trash import purge_expired, trash_region
from tests.test_domain import NOW
from tests.test_notifications import SLOT, configured, ready
from tests.test_notifications import memory_mail as memory_mail
from tests.test_trash_boundaries import _race_before_region_lock

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.usefixtures("memory_mail")]


def test_removing_a_verified_alias_preserves_other_members_and_external_addresses() -> None:
    system, _ = configured()
    removed = User.objects.create(username="removed", email="removed@example.com")
    retained = User.objects.create(username="retained", email="retained@example.com")
    blank = User.objects.create(username="blank", email="")
    removed_membership = Membership.objects.create(tenant=system.tenant, user=removed)
    retained_membership = Membership.objects.create(tenant=system.tenant, user=retained)
    Membership.objects.create(tenant=system.tenant, user=blank)
    EmailAddress.objects.create(user=removed, email="alias@example.com", verified=True)
    schedule = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=["ALIAS@example.com", retained.email, "external@example.com"],
        times=["10:00"],
        language="en",
        now=NOW,
    )
    (delivery_id,) = enqueue_due(SLOT)
    assert schedule.recipient_memberships == {
        "alias@example.com": [removed_membership.pk],
        retained.email: [retained_membership.pk],
    }
    remove_workspace_member(system.member, system.tenant.pk, removed.pk)
    schedule.refresh_from_db()
    assert schedule.recipients == [retained.email, "external@example.com"]
    assert schedule.recipient_memberships == {retained.email: [retained_membership.pk]}
    assert process_notification(delivery_id, SLOT)
    assert mail.outbox[0].bcc == [retained.email, "external@example.com"]


def test_retention_purge_between_delivery_lookup_and_region_lock_cancels_transmission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery = ready()
    schedule = delivery.schedule
    actor = schedule.configured_by
    assert actor is not None
    trash_region(actor, schedule.region.tenant_id, schedule.region_id)
    schedule.region.refresh_from_db()
    deleted_at = schedule.region.deleted_at
    assert deleted_at is not None

    def purge() -> None:
        purge_expired(deleted_at + timedelta(days=31))

    assert (
        _race_before_region_lock(
            monkeypatch,
            "first",
            lambda: process_notification(delivery.pk, SLOT),
            purge,
        )
        is False
    )
    assert not mail.outbox
