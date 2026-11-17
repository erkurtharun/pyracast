import io
import json
from datetime import datetime, timedelta

import pytest
from django.core.management import CommandError, call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test.utils import CaptureQueriesContext

from pyracast.connections.operations import rotate_secret, validate_binding
from pyracast.forecasts.models import (
    CollectionJob,
    CollectionSchedule,
    DispatcherStatus,
    ForecastSnapshot,
)
from pyracast.forecasts.monitoring import delivery_health, record_dispatch_phase
from pyracast.metering.models import EpiasCollectionJob
from pyracast.notifications.models import NotificationDelivery, ReminderSchedule
from pyracast.regions.models import Region
from tests.support.delivery_health import NOW, recent_forecast
from tests.support.domain import FakeAdapter, System, make_system
from tests.support.metering import connected


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("delivery_count", [0, 4])
def test_health_report_keeps_a_bounded_query_budget(delivery_count: int) -> None:
    system = make_system(approved=True)
    recent_forecast(system)
    for phase in DispatcherStatus.Phase.values:
        record_dispatch_phase(phase, NOW)
    schedule = ReminderSchedule.objects.create(
        region=system.region,
        enabled=True,
        effective_from=NOW,
        recipients=["recipient@example.test"],
    )
    for index in range(delivery_count):
        NotificationDelivery.objects.create(
            schedule=schedule,
            binding=system.binding,
            recipients=["recipient@example.test"],
            scheduled_for=NOW - timedelta(minutes=index),
            preparation_deadline=NOW,
            available_at=NOW,
            status="sent",
            prepared_message={"kind": "saved_complete", "tomorrow_complete": True},
        )
    with CaptureQueriesContext(connection) as queries:
        report = delivery_health(NOW, timedelta(minutes=5))
    assert report["healthy"] is True
    assert report["saved_forecast_notifications"] == delivery_count
    assert len(queries) <= 8


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
    recent_forecast(system)
    schedule = ReminderSchedule.objects.create(
        configured_by=system.member,
        region=system.region,
        enabled=True,
        effective_from=NOW,
        recipients=["recipient@example.test"],
    )
    delivery = NotificationDelivery.objects.create(
        schedule=schedule,
        binding=system.binding,
        scheduled_for=NOW,
        preparation_deadline=NOW,
        available_at=NOW,
        status="sending",
        recipients=["recipient@example.test"],
        prepared_message={"kind": "current", "tomorrow_complete": True},
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
    delivery.last_error = "mail_credentials_rejected"
    delivery.save()
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report["mail_authentication_failures"] == 1
    assert report["healthy"] is False


@pytest.mark.django_db(transaction=True)
def test_stored_authentication_failures_become_rejected_credentials_on_migration() -> None:
    system = make_system(approved=True)
    schedule = ReminderSchedule.objects.create(
        region=system.region, enabled=True, effective_from=NOW, recipients=["r@example.test"]
    )
    previous = [("notifications", "0010_notificationdelivery_message_token")]
    executor = MigrationExecutor(connection)
    current = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate(previous)
        delivery_model = executor.loader.project_state(previous).apps.get_model(
            "notifications", "NotificationDelivery"
        )
        for minute, error in enumerate(("mail_authentication_failed", "mail_not_sent")):
            delivery_model.objects.create(
                schedule_id=schedule.pk,
                scheduled_for=NOW - timedelta(minutes=minute),
                available_at=NOW,
                status="retry",
                last_error=error,
            )
    finally:
        MigrationExecutor(connection).migrate(current)

    assert sorted(NotificationDelivery.objects.values_list("last_error", flat=True)) == [
        "mail_credentials_rejected",
        "mail_not_sent",
    ]
    assert delivery_health(NOW, timedelta(minutes=5))["mail_authentication_failures"] == 1


@pytest.mark.django_db(transaction=True)
def test_long_cooldown_stale_forecasts_and_missed_delivery_are_not_healthy() -> None:
    system = make_system(approved=True)
    for name in DispatcherStatus.Phase.values:
        record_dispatch_phase(name, NOW)
    snapshot = recent_forecast(system)
    system.account.request_blocked_until = NOW + timedelta(days=2)
    system.account.save(update_fields=("request_blocked_until",))
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report["provider_cooldowns"] == 1
    assert report["healthy"] is False
    system.account.request_blocked_until = None
    system.account.save(update_fields=("request_blocked_until",))
    ForecastSnapshot.objects.filter(pk=snapshot.pk).update(collected_at=NOW - timedelta(hours=27))
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report["regions_without_recent_forecasts"] == 1
    assert report["healthy"] is False
    CollectionSchedule.objects.create(region=system.region, enabled=False, effective_from=NOW)
    assert delivery_health(NOW, timedelta(minutes=5))["regions_without_recent_forecasts"] == 0
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
        recipients=["recipient@example.test"],
    )
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report["missed_notifications"] == 1
    assert report["healthy"] is False


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
            recipients=[],
        )


@pytest.mark.django_db(transaction=True)
def test_paused_notifications_are_reported_without_turning_health_red() -> None:
    system = make_system(approved=True)
    recent_forecast(system)
    for phase in DispatcherStatus.Phase.values:
        record_dispatch_phase(phase, NOW)
    paused_deliveries(system)
    paused_deliveries(system, NOW - timedelta(days=2))
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report["paused_notifications"] == len(PAUSE_CODES)
    assert report["healthy"] is True


@pytest.mark.django_db(transaction=True)
def test_missed_count_ignores_pause_codes() -> None:
    paused_deliveries(make_system(approved=True))
    assert delivery_health(NOW, timedelta(minutes=5))["missed_notifications"] == 0


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


@pytest.mark.django_db
def test_health_command_bounds_every_statement_to_ten_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def report(_now: datetime, _lag: timedelta) -> dict[str, object]:
        with connection.cursor() as cursor:
            cursor.execute("SHOW statement_timeout")
            row = cursor.fetchone()
        assert row is not None
        return {"healthy": True, "statement_timeout": row[0]}

    monkeypatch.setattr(
        "pyracast.forecasts.management.commands.check_delivery_health.delivery_health", report
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
    for phase in DispatcherStatus.Phase.values:
        record_dispatch_phase(phase, NOW)
    EpiasCollectionJob.objects.update(next_attempt_at=NOW + timedelta(hours=1))
    assert delivery_health(NOW, timedelta(minutes=5))["healthy"] is True
    job = EpiasCollectionJob.objects.first()
    assert job is not None
    job.status = "running"
    job.lease_expires_at = NOW - timedelta(minutes=1) if expired else None
    job.save(update_fields=("status", "lease_expires_at"))
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report["interrupted_metering"] == 1
    assert report["healthy"] is False
    job.lease_expires_at = NOW + timedelta(minutes=1)
    job.save(update_fields=("lease_expires_at",))
    assert delivery_health(NOW, timedelta(minutes=5))["healthy"] is True
    job.lease_expires_at = None
    job.save(update_fields=("lease_expires_at",))
    Region.objects.filter(pk=plant.region_id).update(deleted_at=NOW)
    assert delivery_health(NOW, timedelta(minutes=5))["interrupted_metering"] == 0


@pytest.mark.django_db(transaction=True)
def test_credential_rotation_requires_validation_before_health_recovers() -> None:
    system = make_system(approved=True)
    recent_forecast(system)
    for phase in DispatcherStatus.Phase.values:
        record_dispatch_phase(phase, NOW)
    assert delivery_health(NOW, timedelta(minutes=5))["healthy"] is True
    rotate_secret(system.admin, system.account.pk, "test-only-rotated-health-key")
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report["regions_without_recent_forecasts"] == 0
    assert report["unvalidated_provider_credentials"] == 1
    assert report["healthy"] is False
    validate_binding(system.admin, system.binding.pk, FakeAdapter(), NOW)
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report["unvalidated_provider_credentials"] == 0
    assert report["healthy"] is True


@pytest.mark.django_db(transaction=True)
def test_invalid_saved_recipient_is_an_explicit_health_failure_without_disclosure() -> None:
    system = make_system(approved=True)
    recent_forecast(system)
    for phase in DispatcherStatus.Phase.values:
        record_dispatch_phase(phase, NOW)
    schedule = ReminderSchedule.objects.create(
        configured_by=system.member,
        region=system.region,
        enabled=True,
        effective_from=NOW,
        recipients=["private-invalid-recipient"],
    )
    report = delivery_health(NOW, timedelta(minutes=5))
    assert report["invalid_reminder_recipients"] == 1
    assert report["healthy"] is False
    assert "private-invalid-recipient" not in json.dumps(report)
    schedule.recipients = ["valid@example.test"]
    schedule.save(update_fields=("recipients",))
    assert delivery_health(NOW, timedelta(minutes=5))["healthy"] is True


@pytest.mark.django_db
def test_dispatch_phase_stores_duration_and_published_count() -> None:
    record_dispatch_phase("collection", NOW, duration_ms=1250, published=4)
    record_dispatch_phase("retention", NOW, "OperationalError", duration_ms=8)
    rows = {row.phase: row for row in DispatcherStatus.objects.all()}
    assert (rows["collection"].duration_ms, rows["collection"].published) == (1250, 4)
    assert rows["collection"].succeeded_at == NOW
    assert (rows["retention"].duration_ms, rows["retention"].published) == (8, None)
    assert rows["retention"].succeeded_at is None
