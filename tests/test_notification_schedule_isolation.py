import logging
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import connection
from django.db.models import QuerySet
from rest_framework.test import APIClient

from pyracast.accounts.models import Membership, Tenant
from pyracast.connections.models import ProviderAccount
from pyracast.connections.operations import validate_binding
from pyracast.notifications import runtime
from pyracast.notifications import scheduler as notification_scheduler
from pyracast.notifications.models import NotificationDelivery, ReminderSchedule
from pyracast.notifications.operations import configure_reminders, invalid_reminder_recipient_count
from pyracast.notifications.scheduler import NotificationSchedulingError, enqueue_due
from pyracast.regions.models import Region
from pyracast.regions.operations import approve_region, configure_binding, submit_region
from tests.test_domain import NOW, FakeAdapter, System
from tests.test_notification_recipient_validation import recipient
from tests.test_notifications import SLOT, configured

pytestmark = pytest.mark.django_db(transaction=True)


def another_tenant_schedule(system: System) -> ReminderSchedule:
    user = User.objects.create(username="healthy", email="healthy@example.com")
    tenant = Tenant.objects.create(name="Healthy tenant")
    Membership.objects.create(user=user, tenant=tenant, role=Membership.Role.ADMIN)
    region = submit_region(
        user, tenant.pk, name="Healthy site", latitude=41, longitude=30, timezone="Europe/Istanbul"
    )
    binding = configure_binding(
        system.admin,
        region.pk,
        provider_account_id=system.account.pk,
        site_id="healthy-site",
        capacity_kw=1,
        capacity_semantics_verified=True,
    )
    validate_binding(system.admin, binding.pk, FakeAdapter(), NOW - timedelta(hours=1))
    approve_region(system.admin, region.pk, binding.pk, NOW)
    return configure_reminders(
        user,
        tenant.pk,
        region.pk,
        language="en",
        enabled=True,
        recipients=[],
        times=["10:00"],
        now=NOW,
    )


def test_request_and_application_reject_oversized_recipients_without_changing_schedule() -> None:
    system, schedule = configured()
    address = recipient(255)
    with pytest.raises(ValidationError, match="254"):
        configure_reminders(
            system.member,
            system.tenant.pk,
            system.region.pk,
            language="en",
            enabled=True,
            recipients=[address],
            times=["10:00"],
            now=NOW,
        )
    client = APIClient()
    client.force_authenticate(system.member)
    response = client.put(
        f"/api/tenants/{system.tenant.pk}/regions/{system.region.pk}/reminders/",
        {"enabled": True, "recipients": [address], "times": ["10:00"], "language": "en"},
        format="json",
    )
    assert response.status_code == 400
    schedule.refresh_from_db()
    assert schedule.recipients == ["default@example.com"]


def test_oversized_saved_recipient_does_not_block_another_tenant_or_pending_delivery(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    system, broken = configured()
    healthy = another_tenant_schedule(system)
    address = recipient(255)
    ReminderSchedule.objects.filter(pk=broken.pk).update(recipients=[address])
    actor = MagicMock()
    logger = logging.getLogger("test.notifications.scheduler")
    monkeypatch.setattr("pyracast.notifications.scheduler.logger", logger)
    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(NotificationSchedulingError, match="1 email reminder"),
    ):
        runtime.dispatch_notifications(actor, SLOT)
    deliveries = list(NotificationDelivery.objects.filter(schedule=healthy))
    assert len(deliveries) == 1
    assert deliveries[0].status == "pending"
    assert deliveries[0].recipient == "healthy@example.com"
    actor.send.assert_called_once_with(deliveries[0].pk)
    assert not NotificationDelivery.objects.filter(schedule=broken).exists()
    broken.refresh_from_db()
    assert broken.recipients == [address]
    assert address not in caplog.text
    assert "ValidationError" in caplog.text
    assert invalid_reminder_recipient_count() == 1


def test_slot_database_failure_rolls_back_its_recipients_before_another_tenant_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, broken = configured()
    configure_reminders(
        system.member,
        system.tenant.pk,
        system.region.pk,
        language="en",
        enabled=True,
        recipients=["first@example.com", "second@example.com"],
        times=["10:00"],
        now=NOW,
    )
    healthy = another_tenant_schedule(system)
    original = QuerySet.get_or_create

    def create(queryset: QuerySet[Any], **kwargs: Any) -> tuple[Any, bool]:
        if (
            queryset.model is NotificationDelivery
            and kwargs.get("recipient") == "second@example.com"
        ):
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 / 0")
        return original(queryset, **kwargs)

    monkeypatch.setattr(QuerySet, "get_or_create", create)
    with pytest.raises(NotificationSchedulingError):
        enqueue_due(SLOT)
    assert not NotificationDelivery.objects.filter(schedule=broken).exists()
    assert NotificationDelivery.objects.filter(schedule=healthy, status="pending").count() == 1
    assert connection.get_autocommit()


def test_invalid_recipient_health_count_only_includes_current_approved_schedules() -> None:
    system, schedule = configured()
    assert invalid_reminder_recipient_count() == 0
    ReminderSchedule.objects.filter(pk=schedule.pk).update(recipients=[recipient(255)])
    assert invalid_reminder_recipient_count() == 1
    ReminderSchedule.objects.filter(pk=schedule.pk).update(enabled=False)
    assert invalid_reminder_recipient_count() == 0
    ReminderSchedule.objects.filter(pk=schedule.pk).update(enabled=True)
    Region.objects.filter(pk=system.region.pk).update(status="suspended")
    assert invalid_reminder_recipient_count() == 0
    Region.objects.filter(pk=system.region.pk).update(status="approved", deleted_at=NOW)
    assert invalid_reminder_recipient_count() == 0
    Region.objects.filter(pk=system.region.pk).update(deleted_at=None)
    User.objects.filter(pk=system.member.pk).update(is_active=False)
    assert invalid_reminder_recipient_count() == 1
    User.objects.filter(pk=system.member.pk).update(is_active=True)
    Membership.objects.filter(user=system.member, tenant=system.tenant).delete()
    assert invalid_reminder_recipient_count() == 1
    Membership.objects.create(user=system.member, tenant=system.tenant, role=Membership.Role.ADMIN)
    ProviderAccount.objects.filter(pk=system.account.pk).update(enabled=False)
    assert invalid_reminder_recipient_count() == 0
    ProviderAccount.objects.filter(pk=system.account.pk).update(enabled=True)
    ReminderSchedule.objects.filter(pk=schedule.pk).update(recipients=[None])
    assert invalid_reminder_recipient_count() == 1
    ReminderSchedule.objects.filter(pk=schedule.pk).update(retired_at=NOW)
    assert invalid_reminder_recipient_count() == 0


def test_a_terminated_connection_is_recovered_before_processing_another_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    system, broken = configured()
    healthy = another_tenant_schedule(system)
    original = notification_scheduler._enqueue_schedule

    def interrupt(schedule: ReminderSchedule, now: datetime) -> list[int]:
        if schedule.pk == broken.pk:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                own_pid = cursor.fetchone()[0]
            with (
                psycopg.connect(
                    **connection.get_connection_params(), autocommit=True
                ) as controller,
                controller.cursor() as cursor,
            ):
                cursor.execute("SELECT pg_terminate_backend(%s)", (own_pid,))
                assert cursor.fetchone() == (True,)
        return original(schedule, now)

    monkeypatch.setattr(notification_scheduler, "_enqueue_schedule", interrupt)
    with pytest.raises(NotificationSchedulingError, match="1 email reminder"):
        enqueue_due(SLOT)
    assert not NotificationDelivery.objects.filter(schedule=broken).exists()
    assert NotificationDelivery.objects.filter(schedule=healthy, status="pending").count() == 1
