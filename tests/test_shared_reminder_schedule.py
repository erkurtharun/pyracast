from datetime import datetime, timedelta

import pytest
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import EmailMultiAlternatives
from django.test import Client
from django.utils import timezone

from pyracast.accounts.models import Membership, Tenant, UserLanguagePreference
from pyracast.accounts.operations import reassign_workspace_admin
from pyracast.forecasts.models import RequestAttempt
from pyracast.notifications.jobs import process_notification
from pyracast.notifications.models import NotificationDelivery, ReminderSchedule
from pyracast.notifications.operations import configure_reminders, get_reminder_schedule
from pyracast.notifications.scheduler import enqueue_due
from pyracast.regions.models import Region
from tests.test_domain import NOW, make_system

pytestmark = pytest.mark.django_db(transaction=True)
SLOT = NOW + timedelta(days=1, hours=-6)


def test_transfer_preserves_one_shared_schedule_and_its_deliveries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system = make_system(approved=True)
    system.member.email = "original@example.com"
    system.member.save(update_fields=["email"])
    schedule = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=[],
        times=["10:00"],
        language="tr",
        now=NOW,
    )
    replacement = User.objects.create_user(username="replacement", email="new@example.com")
    Membership.objects.create(user=replacement, tenant=system.tenant)
    old_client, new_client = Client(), Client()
    old_client.force_login(system.member)
    new_client.force_login(replacement)
    url = f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/"
    original = old_client.get(url).json()
    assert new_client.get(url).json() == original
    assert original["recipients"] == ["original@example.com"]
    assert original["language"] == "tr"
    reassign_workspace_admin(
        system.admin, system.tenant.pk, replacement.pk, expected_admin_id=system.member.pk
    )
    assert new_client.get(url).json() == original
    values = {
        "enabled": True,
        "recipients": ["new@example.com"],
        "times": ["11:00"],
        "language": "en",
    }
    assert old_client.put(url, values, content_type="application/json").status_code == 403
    User.objects.filter(pk=system.member.pk).update(is_active=False, email="changed@example.com")
    UserLanguagePreference.objects.create(user=system.member, language="en")
    before = RequestAttempt.objects.count()
    identifiers = enqueue_due(SLOT)
    assert len(identifiers) == 1
    delivery = NotificationDelivery.objects.get(pk=identifiers[0])
    assert delivery.recipient == "original@example.com"
    assert delivery.language == "tr"
    messages: list[EmailMultiAlternatives] = []
    assert process_notification(delivery.pk, SLOT, sender=messages.append)
    assert len(messages) == 1
    assert RequestAttempt.objects.count() == before
    response = new_client.put(url, values, content_type="application/json")
    assert response.status_code == 200, response.content
    schedule.refresh_from_db()
    assert (
        ReminderSchedule.objects.filter(region=system.region, retired_at__isnull=True).count() == 1
    )
    assert schedule.configured_by_id == replacement.pk
    assert schedule.recipients == ["new@example.com"]
    assert schedule.times == ["11:00"]
    assert schedule.language == "en"
    delivery.refresh_from_db()
    assert delivery.language == "tr"
    assert delivery.recipient == "original@example.com"
    assert response.json()["deliveries"][0]["id"] == delivery.pk
    system.member.refresh_from_db()
    system.member.is_active = True
    system.member.save(update_fields=["is_active"])
    assert old_client.get(url).json() == new_client.get(url).json()


def test_schedule_survives_configurer_departure_and_other_workspaces_are_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system = make_system(approved=True)
    schedule = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=["shared@example.com"],
        times=["10:00"],
        language="en",
        now=NOW,
    )
    Membership.objects.filter(user=system.member, tenant=system.tenant).delete()
    schedule.configured_by = None
    schedule.save(update_fields=["configured_by"])
    assert len(enqueue_due(SLOT)) == 1
    delivery = NotificationDelivery.objects.get()
    assert process_notification(delivery.pk, SLOT, sender=lambda message: None)
    other = Tenant.objects.create(name="Other shared workspace")
    actor = User.objects.create_user(username="other-admin", email="other@example.com")
    Membership.objects.create(user=actor, tenant=other, role=Membership.Role.ADMIN)
    region = Region.objects.create(
        tenant=other, name="Other site", latitude=41, longitude=30, timezone="UTC"
    )
    other_schedule = configure_reminders(
        actor,
        other.pk,
        region.pk,
        enabled=False,
        recipients=["private@example.com"],
        times=[],
        language="tr",
        now=NOW,
    )
    with pytest.raises(PermissionDenied):
        get_reminder_schedule(actor, system.tenant.pk, system.region.pk, NOW)
    with pytest.raises(PermissionDenied):
        configure_reminders(
            actor,
            system.tenant.pk,
            system.region.pk,
            enabled=False,
            recipients=["intruder@example.com"],
            times=[],
            language="en",
            now=NOW,
        )
    client = Client()
    client.force_login(actor)
    response = client.get(f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/")
    assert response.status_code == 403
    other_schedule.refresh_from_db()
    assert other_schedule.recipients == ["private@example.com"]
    schedule.refresh_from_db()
    assert schedule.recipients == ["shared@example.com"]


def test_reminder_language_is_explicit_and_empty_recipients_resolve_once() -> None:
    system = make_system()
    system.member.email = "saved@example.com"
    system.member.save(update_fields=["email"])
    with pytest.raises(ValidationError, match="language"):
        configure_reminders(
            system.member,
            system.tenant.pk,
            system.region.pk,
            enabled=False,
            recipients=[],
            times=[],
            language="de",
            now=NOW,
        )
    schedule = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=False,
        recipients=[],
        times=[],
        language="en",
        now=NOW,
    )
    User.objects.filter(pk=system.member.pk).update(email="later@example.com")
    schedule.refresh_from_db()
    assert schedule.recipients == ["saved@example.com"]
    client = Client()
    client.force_login(system.member)
    response = client.put(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/",
        {"enabled": False, "recipients": [], "times": []},
        content_type="application/json",
    )
    assert response.status_code == 400


def test_shared_read_includes_retired_schedule_delivery_history() -> None:
    system = make_system()
    current = configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=False,
        recipients=["current@example.com"],
        times=[],
        language="en",
        now=NOW,
    )
    retired = ReminderSchedule.objects.create(
        region=system.region,
        enabled=False,
        recipients=["historical@example.com"],
        times=["10:00"],
        language="tr",
        effective_from=NOW - timedelta(days=2),
        retired_at=NOW,
    )
    delivery = NotificationDelivery.objects.create(
        schedule=retired,
        recipient="historical@example.com",
        scheduled_for=NOW - timedelta(days=1),
        available_at=NOW - timedelta(days=1),
        status="sent",
        sent_at=NOW - timedelta(days=1),
        language="tr",
    )
    viewer = User.objects.create_user(username="history-viewer")
    Membership.objects.create(user=viewer, tenant=system.tenant)
    assert get_reminder_schedule(viewer, system.tenant.pk, system.region.pk, NOW).pk == current.pk
    client = Client()
    client.force_login(viewer)
    response = client.get(f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/")
    assert response.status_code == 200
    assert response.json()["recipients"] == ["current@example.com"]
    assert response.json()["language"] == "en"
    assert response.json()["deliveries"][0]["id"] == delivery.pk
    assert response.json()["deliveries"][0]["recipient"] == "historical@example.com"


@pytest.mark.parametrize("render_failure", [False, True])
def test_current_slot_preparation_never_reads_or_updates_retired_delivery_content(
    monkeypatch: pytest.MonkeyPatch,
    render_failure: bool,
) -> None:
    monkeypatch.setattr(timezone, "now", lambda: NOW)
    system = make_system(approved=True)
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        enabled=True,
        recipients=["current@example.com"],
        times=["10:00"],
        language="en",
        now=NOW,
    )
    retired = ReminderSchedule.objects.create(
        region=system.region,
        enabled=False,
        recipients=["old@example.com", "pending@example.com"],
        times=["10:00"],
        language="en",
        effective_from=NOW,
        retired_at=NOW,
    )
    original = {
        "format": "plain",
        "kind": "unavailable",
        "prepared_at": (NOW - timedelta(days=50)).isoformat(),
        "retired_evidence": True,
    }
    frozen = NotificationDelivery.objects.create(
        schedule=retired,
        binding=system.binding,
        recipient="old@example.com",
        scheduled_for=SLOT,
        available_at=SLOT,
        status="sent",
        language="en",
        prepared_message=original,
    )
    untouched = NotificationDelivery.objects.create(
        schedule=retired,
        binding=system.binding,
        recipient="pending@example.com",
        scheduled_for=SLOT,
        available_at=SLOT,
        status="pending",
        language="en",
    )
    identifiers = enqueue_due(SLOT)
    assert len(identifiers) == 1
    if render_failure:

        def broken_preparation(delivery: NotificationDelivery, now: datetime) -> bool:
            raise RuntimeError("Cannot render the saved forecast.")

        monkeypatch.setattr("pyracast.notifications.jobs.prepare_delivery", broken_preparation)
    assert process_notification(identifiers[0], SLOT, sender=lambda message: None)
    current = NotificationDelivery.objects.get(pk=identifiers[0])
    assert isinstance(current.prepared_message, dict)
    assert "retired_evidence" not in current.prepared_message
    assert datetime.fromisoformat(current.prepared_message["prepared_at"]) >= SLOT
    frozen.refresh_from_db()
    untouched.refresh_from_db()
    assert frozen.prepared_message == original
    assert frozen.language == "en"
    assert frozen.recipient == "old@example.com"
    assert untouched.prepared_message is None
    assert untouched.status == "pending"
