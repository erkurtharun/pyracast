import io
import json
from datetime import UTC, datetime, timedelta

import pytest
from django.core.management import CommandError, call_command
from django.db import IntegrityError, transaction

from pyracast.forecasts.models import CollectionJob, DispatcherStatus
from pyracast.forecasts.monitoring import delivery_health, record_dispatch_phase
from pyracast.notifications.models import NotificationDelivery, ReminderPreference
from pyracast.regions.models import Region
from tests.test_domain import make_system

NOW = datetime(2026, 11, 12, 13, tzinfo=UTC)


@pytest.mark.django_db
def test_missing_failed_and_stale_dispatch_phases_are_reported() -> None:
    assert delivery_health(NOW, timedelta(minutes=5))["unhealthy_dispatch_phases"] == list(
        DispatcherStatus.Phase.values
    )
    for name in DispatcherStatus.Phase.values:
        record_dispatch_phase(name, NOW - timedelta(minutes=1))
    record_dispatch_phase("collection", NOW, "ConnectionError")
    record_dispatch_phase("metering", NOW - timedelta(minutes=6))
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report["healthy"] is False
    assert report["unhealthy_dispatch_phases"] == ["collection", "metering"]
    assert DispatcherStatus.objects.get(phase="collection").succeeded_at == NOW - timedelta(
        minutes=1
    )
    for name in ("collection", "metering"):
        record_dispatch_phase(name, NOW)
    assert delivery_health(NOW, timedelta(minutes=5))["healthy"] is True
    record_dispatch_phase("retention", NOW, "OperationalError")
    assert delivery_health(NOW, timedelta(minutes=5))["unhealthy_dispatch_phases"] == ["retention"]


@pytest.mark.django_db
def test_first_dispatch_failure_has_no_success_timestamp_and_invalid_phase_is_rejected() -> None:
    record_dispatch_phase("notifications", NOW, "OperationalError")
    assert DispatcherStatus.objects.get().succeeded_at is None
    assert delivery_health(NOW, timedelta(minutes=5))["healthy"] is False
    with pytest.raises(IntegrityError), transaction.atomic():
        record_dispatch_phase("not-a-phase", NOW)


@pytest.mark.django_db(transaction=True)
def test_delivery_health_reports_waiting_work_and_blocked_credentials_without_secrets() -> None:
    system = make_system(approved=True)
    job = CollectionJob.objects.get()
    job.available_at = NOW - timedelta(minutes=10)
    job.save(update_fields=("available_at",))
    account = system.account
    account.encrypted_api_key = "encrypted:v1:private-health-test-key"
    account.authentication_failed_revision = account.credential_revision
    account.request_blocked_until = NOW + timedelta(minutes=2)
    account.save(
        update_fields=(
            "encrypted_api_key",
            "authentication_failed_revision",
            "request_blocked_until",
        )
    )
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report["overdue_collections"] == 1
    assert report["blocked_provider_credentials"] == 1
    assert report["provider_cooldowns"] == 1
    assert account.encrypted_api_key not in json.dumps(report)
    Region.objects.filter(pk=system.region.pk).update(deleted_at=NOW)
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report["blocked_provider_credentials"] == report["provider_cooldowns"] == 0


@pytest.mark.django_db(transaction=True)
def test_incomplete_and_interrupted_deliveries_remain_visible_to_monitoring() -> None:
    system = make_system(approved=True)
    for name in DispatcherStatus.Phase.values:
        record_dispatch_phase(name, NOW)
    CollectionJob.objects.update(status="succeeded")
    preference = ReminderPreference.objects.create(
        user=system.member, region=system.region, enabled=True, effective_from=NOW
    )
    delivery = NotificationDelivery.objects.create(
        preference=preference,
        binding=system.binding,
        scheduled_for=NOW,
        available_at=NOW,
        status="sending",
        recipient="recipient@example.test",
        lease_expires_at=None,
    )
    assert delivery_health(NOW, timedelta(minutes=5))["interrupted_notifications"] == 1
    delivery.status = "sent"
    delivery.prepared_message = {"kind": "saved_partial", "tomorrow_complete": False}
    delivery.save()
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report["healthy"] is False
    assert report["partial_forecasts"] == report["saved_forecast_notifications"] == 1
    delivery.prepared_message = {"kind": "saved_complete", "tomorrow_complete": True}
    delivery.save()
    assert delivery_health(NOW, timedelta(minutes=5))["healthy"] is True
    delivery.status = "retry"
    delivery.last_error = "mail_authentication_failed"
    delivery.save()
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report["mail_authentication_failures"] == 1
    assert report["healthy"] is False


@pytest.mark.django_db
def test_health_command_emits_json_and_nonzero_for_degraded_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pyracast.forecasts.management.commands.check_delivery_health.timezone.now", lambda: NOW
    )
    output = io.StringIO()
    with pytest.raises(CommandError, match="needs attention"):
        call_command("check_delivery_health", stdout=output)
    assert json.loads(output.getvalue())["healthy"] is False
    for name in DispatcherStatus.Phase.values:
        record_dispatch_phase(name, NOW)
    output = io.StringIO()
    call_command("check_delivery_health", stdout=output)
    assert json.loads(output.getvalue())["healthy"] is True
    with pytest.raises(CommandError, match="must be positive"):
        call_command("check_delivery_health", max_lag_minutes=0)
