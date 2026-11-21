import io
import json
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from django.core.management import CommandError, call_command
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext

from pyracast.connections.operations import rotate_secret
from pyracast.forecasts.models import (
    CollectionJob,
    CollectionSchedule,
    DispatcherPhase,
    DispatcherStatus,
    ForecastSnapshot,
)
from pyracast.forecasts.validation import validate_binding
from pyracast.metering.models import EpiasCollectionJob
from pyracast.notifications.models import NotificationDelivery, ReminderSchedule
from pyracast.orchestration.delivery_health import delivery_health
from pyracast.orchestration.dispatcher import record_dispatch_phase
from pyracast.regions.models import Region
from tests.support.delivery_health import NOW, answering_broker, recent_forecast
from tests.support.domain import FakeAdapter, System, make_system
from tests.support.invariants import stored_again
from tests.support.metering import connected
from tests.support.notifications import store


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("delivery_count", [0, 4])
def test_health_report_keeps_a_bounded_query_budget(delivery_count: int) -> None:
    system = make_system(approved=True)
    recent_forecast(system)
    for phase in DispatcherPhase.values:
        record_dispatch_phase(phase, NOW)
    schedule = ReminderSchedule.objects.create(
        region=system.region,
        enabled=True,
        effective_from=NOW,
    )
    store(schedule, ["recipient@example.test"])
    for index in range(delivery_count):
        NotificationDelivery.objects.create(
            schedule=schedule,
            binding=system.binding,
            scheduled_for=NOW - timedelta(minutes=index),
            preparation_deadline=NOW,
            available_at=NOW,
            status="sent",
            forecast_kind="saved_complete",
            forecast_complete=True,
            prepared_at=NOW,
        )
    with CaptureQueriesContext(connection) as queries:
        report = delivery_health(NOW, timedelta(minutes=5))
    assert report.healthy is True
    assert report.counts["saved_forecast_notifications"] == delivery_count
    assert len(queries) <= 10


@pytest.mark.django_db
def test_missing_failed_and_stale_dispatch_phases_are_reported() -> None:
    assert delivery_health(NOW, timedelta(minutes=5)).unhealthy_phases == list(
        DispatcherPhase.values
    )
    for name in DispatcherPhase.values:
        record_dispatch_phase(name, NOW - timedelta(minutes=1))
    record_dispatch_phase("collection", NOW, "ConnectionError")
    record_dispatch_phase("metering", NOW - timedelta(minutes=6))
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report.healthy is False
    assert report.unhealthy_phases == ["collection", "metering"]
    assert DispatcherStatus.objects.get(phase="collection").succeeded_at == NOW - timedelta(
        minutes=1
    )
    for name in ("collection", "metering"):
        record_dispatch_phase(name, NOW)
    assert delivery_health(NOW, timedelta(minutes=5)).healthy is True
    record_dispatch_phase("retention", NOW, "OperationalError")
    assert delivery_health(NOW, timedelta(minutes=5)).unhealthy_phases == ["retention"]


@pytest.mark.django_db
def test_first_dispatch_failure_has_no_success_timestamp_and_invalid_phase_is_rejected() -> None:
    record_dispatch_phase("notifications", NOW, "OperationalError")
    assert DispatcherStatus.objects.get().succeeded_at is None
    assert delivery_health(NOW, timedelta(minutes=5)).healthy is False
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
    assert report.counts["overdue_collections"] == 1
    assert report.counts["blocked_provider_credentials"] == 1
    assert report.counts["provider_cooldowns"] == 1
    assert account.encrypted_api_key not in json.dumps(report.as_json())
    Region.objects.filter(pk=system.region.pk).update(deleted_at=NOW)
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report.counts["blocked_provider_credentials"] == report.counts["provider_cooldowns"] == 0


@pytest.mark.django_db(transaction=True)
def test_incomplete_and_interrupted_deliveries_remain_visible_to_monitoring() -> None:
    system = make_system(approved=True)
    for name in DispatcherPhase.values:
        record_dispatch_phase(name, NOW)
    recent_forecast(system)
    schedule = ReminderSchedule.objects.create(
        configured_by=system.member,
        region=system.region,
        enabled=True,
        effective_from=NOW,
    )
    delivery = NotificationDelivery.objects.create(
        schedule=schedule,
        binding=system.binding,
        scheduled_for=NOW,
        preparation_deadline=NOW,
        available_at=NOW,
        status="sending",
        prepared_message={},
        forecast_kind="current",
        forecast_complete=True,
        prepared_at=NOW,
        lease_token=uuid.uuid4(),
        lease_expires_at=NOW,
    )
    assert delivery_health(NOW, timedelta(minutes=5)).counts["interrupted_notifications"] == 1
    delivery.status = "sent"
    delivery.lease_token = delivery.lease_expires_at = None
    delivery.forecast_kind, delivery.forecast_complete = "saved_partial", False
    delivery.save()
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report.healthy is False
    assert report.counts["partial_forecasts"] == report.counts["saved_forecast_notifications"] == 1
    delivery.forecast_kind, delivery.forecast_complete = "saved_complete", True
    delivery.save()
    assert delivery_health(NOW, timedelta(minutes=5)).healthy is True
    delivery.status = "retry"
    delivery.last_error = "mail_credentials_rejected"
    delivery.save()
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report.counts["mail_authentication_failures"] == 1
    assert report.healthy is False


@pytest.mark.django_db(transaction=True)
def test_long_cooldown_stale_forecasts_and_missed_delivery_are_not_healthy() -> None:
    system = make_system(approved=True)
    for name in DispatcherPhase.values:
        record_dispatch_phase(name, NOW)
    snapshot = recent_forecast(system)
    system.account.request_blocked_until = NOW + timedelta(days=2)
    system.account.save(update_fields=("request_blocked_until",))
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report.counts["provider_cooldowns"] == 1
    assert report.healthy is False
    system.account.request_blocked_until = None
    system.account.save(update_fields=("request_blocked_until",))
    stored_again(
        ForecastSnapshot.objects.get(pk=snapshot.pk), collected_at=NOW - timedelta(hours=27)
    )
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report.counts["regions_without_recent_forecasts"] == 1
    assert report.healthy is False
    CollectionSchedule.objects.create(region=system.region, enabled=False, effective_from=NOW)
    assert (
        delivery_health(NOW, timedelta(minutes=5)).counts["regions_without_recent_forecasts"] == 0
    )
    schedule = ReminderSchedule.objects.create(
        configured_by=system.member, region=system.region, enabled=True, effective_from=NOW
    )
    NotificationDelivery.objects.create(
        schedule=schedule,
        binding=system.binding,
        scheduled_for=NOW,
        preparation_deadline=NOW,
        available_at=NOW,
        status="skipped",
        last_error="missed_schedule",
    )
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report.counts["missed_notifications"] == 1
    assert report.healthy is False


PAUSE_CODES = ("recipient_limit_exceeded", "no_recipients", "invalid_recipient")


def paused_deliveries(system: System, scheduled_for: datetime = NOW) -> None:
    schedule, _ = ReminderSchedule.objects.get_or_create(
        region=system.region, defaults={"enabled": True, "effective_from": NOW}
    )
    for index, code in enumerate(PAUSE_CODES):
        NotificationDelivery.objects.create(
            schedule=schedule,
            binding=system.binding,
            scheduled_for=scheduled_for - timedelta(minutes=index),
            preparation_deadline=scheduled_for,
            available_at=NOW,
            status="skipped",
            last_error=code,
        )


@pytest.mark.django_db(transaction=True)
def test_paused_notifications_are_reported_without_turning_health_red() -> None:
    system = make_system(approved=True)
    recent_forecast(system)
    for phase in DispatcherPhase.values:
        record_dispatch_phase(phase, NOW)
    paused_deliveries(system)
    paused_deliveries(system, NOW - timedelta(days=2))
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report.counts["paused_notifications"] == len(PAUSE_CODES)
    assert report.healthy is True


@pytest.mark.django_db(transaction=True)
def test_missed_count_ignores_pause_codes() -> None:
    paused_deliveries(make_system(approved=True))
    assert delivery_health(NOW, timedelta(minutes=5)).counts["missed_notifications"] == 0


@pytest.mark.django_db
def test_health_command_emits_json_and_nonzero_for_degraded_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pyracast.orchestration.management.commands.check_delivery_health.timezone.now", lambda: NOW
    )
    answering_broker(monkeypatch)
    output = io.StringIO()
    with pytest.raises(CommandError, match="needs attention"):
        call_command("check_delivery_health", stdout=output)
    assert json.loads(output.getvalue())["healthy"] is False
    for name in DispatcherPhase.values:
        record_dispatch_phase(name, NOW)
    output = io.StringIO()
    call_command("check_delivery_health", stdout=output)
    assert json.loads(output.getvalue())["healthy"] is True
    with pytest.raises(CommandError, match="must be positive"):
        call_command("check_delivery_health", max_lag_minutes=0)


@pytest.mark.django_db
def test_health_command_bounds_every_statement_to_ten_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def report(_now: datetime, _lag: timedelta, **_options: object) -> SimpleNamespace:
        with connection.cursor() as cursor:
            cursor.execute("SHOW statement_timeout")
            row = cursor.fetchone()
        assert row is not None
        return SimpleNamespace(
            healthy=True, as_json=lambda: {"healthy": True, "statement_timeout": row[0]}
        )

    monkeypatch.setattr(
        "pyracast.orchestration.management.commands.check_delivery_health.delivery_health", report
    )
    output = io.StringIO()
    call_command("check_delivery_health", stdout=output)
    assert json.loads(output.getvalue())["statement_timeout"] == "10s"


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.django_db(transaction=True)
def test_interrupted_metering_is_unhealthy_even_with_recent_forecasts_and_healthy_dispatch(
    monkeypatch: pytest.MonkeyPatch, expired: bool
) -> None:
    system, plant, _ = connected(monkeypatch)
    recent_forecast(system)
    for phase in DispatcherPhase.values:
        record_dispatch_phase(phase, NOW)
    EpiasCollectionJob.objects.update(available_at=NOW + timedelta(hours=1))
    assert delivery_health(NOW, timedelta(minutes=5)).healthy is True
    job = EpiasCollectionJob.objects.first()
    assert job is not None
    job.status = "running"
    job.lease_token = uuid.uuid4()
    job.lease_expires_at = NOW - timedelta(minutes=1) if expired else NOW
    job.save(update_fields=("status", "lease_token", "lease_expires_at"))
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report.counts["interrupted_metering"] == 1
    assert report.healthy is False
    job.lease_expires_at = NOW + timedelta(minutes=1)
    job.save(update_fields=("lease_expires_at",))
    assert delivery_health(NOW, timedelta(minutes=5)).healthy is True
    job.lease_expires_at = NOW
    job.save(update_fields=("lease_expires_at",))
    Region.objects.filter(pk=plant.region_id).update(deleted_at=NOW)
    assert delivery_health(NOW, timedelta(minutes=5)).counts["interrupted_metering"] == 0


@pytest.mark.django_db(transaction=True)
def test_credential_rotation_requires_validation_before_health_recovers() -> None:
    system = make_system(approved=True)
    recent_forecast(system)
    for phase in DispatcherPhase.values:
        record_dispatch_phase(phase, NOW)
    assert delivery_health(NOW, timedelta(minutes=5)).healthy is True
    rotate_secret(system.admin, system.account.pk, "test-only-rotated-health-key")
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report.counts["regions_without_recent_forecasts"] == 0
    assert report.counts["unvalidated_provider_credentials"] == 1
    assert report.healthy is False
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW)
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report.counts["unvalidated_provider_credentials"] == 0
    assert report.healthy is True


@pytest.mark.django_db(transaction=True)
def test_invalid_saved_recipient_is_an_explicit_health_failure_without_disclosure() -> None:
    system = make_system(approved=True)
    recent_forecast(system)
    for phase in DispatcherPhase.values:
        record_dispatch_phase(phase, NOW)
    schedule = ReminderSchedule.objects.create(
        configured_by=system.member,
        region=system.region,
        enabled=True,
        effective_from=NOW,
    )
    store(schedule, ["private-invalid-recipient"])
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report.counts["invalid_reminder_recipients"] == 1
    assert report.healthy is False
    assert "private-invalid-recipient" not in json.dumps(report.as_json())
    store(schedule, ["valid@example.test"])
    assert delivery_health(NOW, timedelta(minutes=5)).healthy is True


@pytest.mark.django_db
def test_dispatch_phase_stores_duration_and_published_count() -> None:
    record_dispatch_phase("collection", NOW, duration_ms=1250, published=4)
    record_dispatch_phase("retention", NOW, "OperationalError", duration_ms=8)
    rows = {row.phase: row for row in DispatcherStatus.objects.all()}
    assert (rows["collection"].duration_ms, rows["collection"].published) == (1250, 4)
    assert rows["collection"].succeeded_at == NOW
    assert (rows["retention"].duration_ms, rows["retention"].published) == (8, None)
    assert rows["retention"].succeeded_at is None
