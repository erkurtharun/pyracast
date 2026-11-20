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

from pyracast.accounts.models import Membership, Tenant, WorkspaceRole
from pyracast.background.failures import ItemsFailedError
from pyracast.connections.models import ProviderAccount
from pyracast.forecasts.validation import validate_binding
from pyracast.notifications import runtime
from pyracast.notifications import scheduler as notification_scheduler
from pyracast.notifications.models import NotificationDelivery, ReminderSchedule
from pyracast.notifications.operations import configure_reminders, invalid_recipient_region_ids
from pyracast.notifications.scheduler import enqueue_due
from pyracast.plants.approval import approve_region
from pyracast.regions.models import Region
from pyracast.regions.operations import configure_binding, submit_region
from tests.support.domain import NOW, FakeAdapter, System
from tests.support.notification_recipient_validation import recipient
from tests.support.notifications import SLOT, candidates, configured, schedule_addresses, store

pytestmark = pytest.mark.django_db(transaction=True)


def another_tenant_schedule(system: System) -> ReminderSchedule:
    user = User.objects.create(username="healthy", email="healthy@example.com")
    tenant = Tenant.objects.create(name="Healthy tenant")
    Membership.objects.create(user=user, tenant=tenant, role=WorkspaceRole.ADMIN)
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
        recipients=[user.email],
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
        {
            "enabled": True,
            "include_workspace_members": False,
            "recipients": [address],
            "times": ["10:00"],
            "language": "en",
            "expected_revision": 1,
        },
        format="json",
    )
    assert response.status_code == 400
    schedule.refresh_from_db()
    assert schedule_addresses(schedule) == ["default@example.com"]


def test_invalid_saved_recipient_records_a_skipped_slot_without_blocking_another_tenant(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    system, broken = configured()
    healthy = another_tenant_schedule(system)
    address = recipient(255)
    store(broken, [address])
    actor = MagicMock()
    logger = logging.getLogger("test.notifications.scheduler")
    monkeypatch.setattr("pyracast.notifications.scheduler.logger", logger)
    with caplog.at_level(logging.INFO):
        runtime.dispatch_notifications(actor, MagicMock(), SLOT)
    deliveries = list(NotificationDelivery.objects.filter(schedule=healthy))
    assert len(deliveries) == 1
    assert deliveries[0].status == "pending"
    assert candidates(deliveries[0]) == ["healthy@example.com"]
    paused = NotificationDelivery.objects.get(schedule=broken)
    assert (paused.status, paused.last_error) == ("skipped", "invalid_recipient")
    assert (candidates(paused), paused.snapshot_id) == ([], None)
    actor.send.assert_called_once_with(deliveries[0].pk)
    broken.refresh_from_db()
    assert schedule_addresses(broken) == [address]
    assert address not in caplog.text
    assert f"schedule_id={broken.pk} reason=invalid_recipient" in caplog.text
    assert len(invalid_recipient_region_ids()) == 1


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
    original = QuerySet.create

    def create(queryset: QuerySet[Any], **kwargs: Any) -> Any:
        if queryset.model is NotificationDelivery and kwargs.get("schedule") == broken:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 / 0")
        return original(queryset, **kwargs)

    monkeypatch.setattr(QuerySet, "create", create)
    with pytest.raises(ItemsFailedError):
        enqueue_due(SLOT)
    assert not NotificationDelivery.objects.filter(schedule=broken).exists()
    assert NotificationDelivery.objects.filter(schedule=healthy, status="pending").count() == 1
    assert connection.get_autocommit()


def test_invalid_recipient_health_count_only_includes_current_approved_schedules() -> None:
    system, schedule = configured()
    assert len(invalid_recipient_region_ids()) == 0
    store(schedule, [recipient(255)])
    assert len(invalid_recipient_region_ids()) == 1
    ReminderSchedule.objects.filter(pk=schedule.pk).update(enabled=False)
    assert len(invalid_recipient_region_ids()) == 0
    ReminderSchedule.objects.filter(pk=schedule.pk).update(enabled=True)
    Region.objects.filter(pk=system.region.pk).update(status="suspended")
    assert len(invalid_recipient_region_ids()) == 0
    Region.objects.filter(pk=system.region.pk).update(status="approved", deleted_at=NOW)
    assert len(invalid_recipient_region_ids()) == 0
    Region.objects.filter(pk=system.region.pk).update(deleted_at=None)
    User.objects.filter(pk=system.member.pk).update(is_active=False)
    assert len(invalid_recipient_region_ids()) == 1
    User.objects.filter(pk=system.member.pk).update(is_active=True)
    Membership.objects.filter(user=system.member, tenant=system.tenant).delete()
    assert len(invalid_recipient_region_ids()) == 1
    Membership.objects.create(user=system.member, tenant=system.tenant, role=WorkspaceRole.ADMIN)
    ProviderAccount.objects.filter(pk=system.account.pk).update(enabled=False)
    assert len(invalid_recipient_region_ids()) == 0
    ProviderAccount.objects.filter(pk=system.account.pk).update(enabled=True)
    assert len(invalid_recipient_region_ids()) == 1


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
    with pytest.raises(ItemsFailedError, match="1 item"):
        enqueue_due(SLOT)
    assert not NotificationDelivery.objects.filter(schedule=broken).exists()
    assert NotificationDelivery.objects.filter(schedule=healthy, status="pending").count() == 1


def test_a_failing_schedule_is_logged_with_its_origin_and_the_others_are_published(
    monkeypatch: pytest.MonkeyPatch, pyracast_logs: pytest.LogCaptureFixture
) -> None:
    system, broken = configured()
    healthy = another_tenant_schedule(system)
    original = notification_scheduler._enqueue_schedule

    def enqueue(schedule: ReminderSchedule, now: datetime) -> list[int]:
        if schedule.pk == broken.pk:
            raise RuntimeError("private schedule detail")
        return original(schedule, now)

    monkeypatch.setattr(notification_scheduler, "_enqueue_schedule", enqueue)
    actor = MagicMock()
    with pytest.raises(Exception, match=r"^1 "):
        runtime.dispatch_notifications(actor, MagicMock(), SLOT)
    delivery = NotificationDelivery.objects.get(schedule=healthy)
    actor.send.assert_called_once_with(delivery.pk)
    assert f"item=schedule:{broken.pk}" in pyracast_logs.text
    assert "origin=pyracast." in pyracast_logs.text
    assert "private schedule detail" not in pyracast_logs.text


def test_a_failing_schedule_leaves_the_mail_publication_and_code_erasure_of_its_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyracast.accounts.mail_intents import enqueue_mail
    from pyracast.accounts.models import OutboundMail, OutboundMailKind

    system, broken = configured()
    healthy = another_tenant_schedule(system)
    original = notification_scheduler._enqueue_schedule

    def enqueue(schedule: ReminderSchedule, now: datetime) -> list[int]:
        if schedule.pk == broken.pk:
            raise RuntimeError("private schedule detail")
        return original(schedule, now)

    monkeypatch.setattr(notification_scheduler, "_enqueue_schedule", enqueue)
    for recipient_address, asked in (("due@example.com", 5), ("expired@example.com", 20)):
        enqueue_mail(
            OutboundMailKind.PASSWORD_RESET_CODE,
            recipient_address,
            language="en",
            code="ABCD-EFGH",
            expires_at=SLOT - timedelta(minutes=asked - 10),
            now=SLOT - timedelta(minutes=asked),
        )
    due, expired = OutboundMail.objects.order_by("pk")
    actor, mail_actor = MagicMock(), MagicMock()

    with pytest.raises(ItemsFailedError) as raised:
        runtime.dispatch_notifications(actor, mail_actor, SLOT)

    actor.send.assert_called_once_with(NotificationDelivery.objects.get(schedule=healthy).pk)
    mail_actor.send.assert_called_once_with(due.pk)
    expired.refresh_from_db()
    assert (expired.status, expired.last_error, expired.sealed_code) == ("skipped", "expired", "")
    assert (raised.value.count, raised.value.published) == (1, 2)
