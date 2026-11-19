import uuid
from datetime import UTC, datetime, timedelta

import pytest
from django.db import connection

from pyracast.admin_health_queries import (
    collection_jobs_for_issue,
    metering_jobs_for_issue,
    notification_deliveries_for_issue,
    regions_for_issue,
)
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.models import (
    CollectionJob,
    CollectionSchedule,
    ForecastResult,
    ForecastSnapshot,
)
from pyracast.forecasts.monitoring import delivery_health
from pyracast.metering.models import EpiasCollectionJob
from pyracast.notifications.models import NotificationDelivery, ReminderSchedule
from pyracast.notifications.scheduler import PREPARATION_GRACE
from pyracast.regions.models import Region
from tests.support.domain import System, make_system
from tests.support.metering import connected
from tests.support.notifications import store_recipients

pytestmark = pytest.mark.django_db(transaction=True)
NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)
LAG = timedelta(minutes=5)
RECENT = NOW - timedelta(days=1)
BEFORE = timedelta(microseconds=1)
PREPARED = {"kind": "current", "tomorrow_complete": True}


def another_region(
    system: System, name: str, *, account: ProviderAccount | None = None
) -> tuple[Region, SiteBinding]:
    region = Region.objects.create(
        tenant=system.tenant,
        name=name,
        latitude=40 + Region.objects.count() / 100,
        longitude=29,
        timezone="Europe/Istanbul",
    )
    binding = SiteBinding.objects.create(
        region=region,
        provider_account=account or system.account,
        region_revision=1,
        site_id=name,
        capacity_kw=1,
        latitude=region.latitude,
        longitude=region.longitude,
        timezone=region.timezone,
        validated_at=NOW,
        validated_credential_revision=1,
    )
    region.status = "approved"
    region.approved_binding = binding
    region.save(update_fields=["status", "approved_binding"])
    return region, binding


def collection(
    binding: SiteBinding,
    status: str,
    *,
    scheduled: datetime | None = None,
    available: datetime = NOW,
    lease: datetime | None = None,
) -> CollectionJob:
    unique_slot = NOW - timedelta(days=30, seconds=CollectionJob.objects.count())
    return CollectionJob.objects.create(
        binding=binding,
        scheduled_for=scheduled or unique_slot,
        target_date=NOW.date(),
        available_at=available,
        status=status,
        lease_token=None if lease is None else uuid.uuid4(),
        lease_expires_at=lease,
    )


@pytest.mark.parametrize(
    "issue", ["overdue_collections", "interrupted_collections", "failed_collections"]
)
def test_collection_filters_match_counter_boundaries_and_find_failures_behind_new_successes(
    issue: str,
) -> None:
    system = make_system(approved=True)
    CollectionJob.objects.all().delete()
    rows = {
        "pending": collection(system.binding, "pending", available=NOW - LAG - BEFORE),
        "retry": collection(system.binding, "retry", available=NOW - LAG - BEFORE),
        "cutoff": collection(system.binding, "pending", available=NOW - LAG),
        "expired_lease": collection(system.binding, "running", lease=NOW),
        "live_lease": collection(system.binding, "running", lease=NOW + BEFORE),
        "failed": collection(system.binding, "failed", scheduled=RECENT),
        "old_failed": collection(system.binding, "failed", scheduled=RECENT - BEFORE),
        "future_failed": collection(system.binding, "failed", scheduled=NOW + timedelta(hours=1)),
    }
    for index in range(25):
        collection(system.binding, "succeeded", scheduled=NOW + timedelta(minutes=index))
    deleted, deleted_binding = another_region(system, "Deleted")
    deleted.deleted_at = NOW
    deleted.save(update_fields=["deleted_at"])
    collection(deleted_binding, "failed", scheduled=RECENT)
    expected = {
        "overdue_collections": {rows["pending"].pk, rows["retry"].pk},
        "interrupted_collections": {rows["expired_lease"].pk},
        "failed_collections": {rows["failed"].pk, rows["future_failed"].pk},
    }[issue]
    matches = collection_jobs_for_issue(issue, NOW).order_by("-id")[:20]
    assert set(matches.values_list("pk", flat=True)) == expected
    assert collection_jobs_for_issue(issue, NOW).count() == delivery_health(NOW, LAG)[issue]
    baseline = collection_jobs_for_issue("unknown", NOW)
    assert baseline.count() == len(rows) + 25
    assert set(
        collection_jobs_for_issue("failed_notifications", NOW).values_list("pk", flat=True)
    ) == set(baseline.values_list("pk", flat=True))
    assert collection_jobs_for_issue("overdue_collections", NOW, timedelta(minutes=6)).count() == 0


def delivery(
    schedule: ReminderSchedule,
    status: str,
    *,
    scheduled: datetime = NOW,
    available: datetime = NOW,
    lease: datetime | None = None,
    error: str = "",
    message: dict[str, object] | None = None,
) -> NotificationDelivery:
    count = NotificationDelivery.objects.count()
    region = Region.objects.create(
        tenant_id=schedule.region.tenant_id,
        name=f"Delivery {count}",
        latitude=-40 - count / 100,
        longitude=29,
        timezone="Europe/Istanbul",
        deleted_at=schedule.region.deleted_at,
    )
    return NotificationDelivery.objects.create(
        schedule=ReminderSchedule.objects.create(region=region, effective_from=NOW),
        binding=SiteBinding.objects.get(region=schedule.region),
        scheduled_for=scheduled,
        preparation_deadline=scheduled + PREPARATION_GRACE,
        available_at=available,
        status=status,
        lease_token=None if lease is None else uuid.uuid4(),
        lease_expires_at=lease,
        last_error=error,
        prepared_message=message,
    )


@pytest.mark.parametrize(
    "issue",
    [
        "overdue_notifications",
        "interrupted_notifications",
        "failed_notifications",
        "missed_notifications",
        "unavailable_forecasts",
        "partial_forecasts",
        "mail_authentication_failures",
        "paused_notifications",
    ],
)
def test_notification_filters_match_counter_boundaries_across_schedules(
    issue: str,
) -> None:
    system = make_system(approved=True)
    schedule = ReminderSchedule.objects.create(region=system.region, effective_from=NOW)
    rows = {
        "pending": delivery(schedule, "pending", available=NOW - LAG - BEFORE),
        "retry": delivery(schedule, "retry", available=NOW - LAG - BEFORE),
        "cutoff": delivery(schedule, "pending", available=NOW - LAG),
        "expired_lease": delivery(schedule, "sending", lease=NOW, message=PREPARED),
        "live_lease": delivery(schedule, "sending", lease=NOW + BEFORE, message=PREPARED),
        "failed": delivery(schedule, "failed", scheduled=RECENT),
        "uncertain": delivery(schedule, "uncertain"),
        "old_failed": delivery(schedule, "failed", scheduled=RECENT - BEFORE),
        "missed": delivery(schedule, "skipped", error="missed_schedule"),
        "deadline": delivery(schedule, "failed", error="delivery_deadline_exceeded"),
        "ignored_error": delivery(schedule, "sent", error="missed_schedule"),
        "old_missed": delivery(
            schedule, "skipped", scheduled=RECENT - BEFORE, error="missed_schedule"
        ),
        "partial": delivery(
            schedule, "sent", scheduled=RECENT, message={"tomorrow_complete": False}
        ),
        "complete": delivery(schedule, "sent", message={"tomorrow_complete": True}),
        "missing": delivery(schedule, "sent", message={"kind": "unavailable"}),
        "auth": delivery(schedule, "retry", error="mail_credentials_rejected"),
        "revoked": delivery(schedule, "failed", error="mail_authorization_revoked"),
        "old_auth": delivery(
            schedule, "retry", scheduled=RECENT - BEFORE, error="mail_credentials_rejected"
        ),
        "old_missing": delivery(
            schedule,
            "sent",
            scheduled=RECENT - BEFORE,
            message={"kind": "unavailable", "tomorrow_complete": False},
        ),
        "over_limit": delivery(schedule, "skipped", error="recipient_limit_exceeded"),
        "no_recipients": delivery(schedule, "skipped", scheduled=RECENT, error="no_recipients"),
        "invalid": delivery(schedule, "skipped", error="invalid_recipient"),
        "old_paused": delivery(
            schedule, "skipped", scheduled=RECENT - BEFORE, error="no_recipients"
        ),
    }
    for _ in range(25):
        delivery(schedule, "sent")
    deleted, _ = another_region(system, "Deleted")
    deleted.deleted_at = NOW
    deleted.save(update_fields=["deleted_at"])
    deleted_schedule = ReminderSchedule.objects.create(region=deleted, effective_from=NOW)
    delivery(deleted_schedule, "failed")
    names = {
        "overdue_notifications": ("pending", "retry"),
        "interrupted_notifications": ("expired_lease",),
        "failed_notifications": ("failed", "uncertain", "deadline", "revoked"),
        "missed_notifications": ("missed", "deadline"),
        "unavailable_forecasts": ("missing",),
        "partial_forecasts": ("partial",),
        "mail_authentication_failures": ("auth", "revoked"),
        "paused_notifications": ("over_limit", "no_recipients", "invalid"),
    }[issue]
    assert set(
        notification_deliveries_for_issue(issue, NOW)
        .order_by("-id")[:20]
        .values_list("pk", flat=True)
    ) == {rows[name].pk for name in names}
    assert notification_deliveries_for_issue(issue, NOW).count() == delivery_health(NOW, LAG)[issue]
    assert notification_deliveries_for_issue("unknown", NOW).count() == len(rows) + 25
    assert notification_deliveries_for_issue("failed_collections", NOW).count() == len(rows) + 25
    assert (
        notification_deliveries_for_issue(
            "overdue_notifications", NOW, timedelta(minutes=6)
        ).count()
        == 0
    )


@pytest.mark.parametrize("issue", ["overdue_metering", "interrupted_metering", "failed_metering"])
def test_metering_filters_match_counter_boundaries_and_find_failures_behind_new_successes(
    monkeypatch: pytest.MonkeyPatch, issue: str
) -> None:
    _, plant, epias = connected(monkeypatch)
    template = EpiasCollectionJob.objects.first()
    assert template is not None
    EpiasCollectionJob.objects.all().delete()

    def metering(
        status: str,
        *,
        scheduled: datetime = NOW,
        available: datetime = NOW,
        lease: datetime | None = None,
    ) -> EpiasCollectionJob:
        return EpiasCollectionJob.objects.create(
            connection=epias,
            plant=plant,
            configuration=template.configuration,
            connection_revision=epias.revision,
            provider_plant={},
            target_date=NOW.date() - timedelta(days=EpiasCollectionJob.objects.count()),
            scheduled_for=scheduled,
            available_at=available,
            status=status,
            lease_token=None if lease is None else uuid.uuid4(),
            lease_expires_at=lease,
        )

    rows = {
        "pending": metering("pending", available=NOW - LAG - BEFORE),
        "cutoff": metering("pending", available=NOW - LAG),
        "expired_lease": metering("running", lease=NOW),
        "live_lease": metering("running", lease=NOW + BEFORE),
        "failed": metering("failed", scheduled=RECENT),
        "old_failed": metering("failed", scheduled=RECENT - BEFORE),
        "future_failed": metering("failed", scheduled=NOW + timedelta(hours=1)),
    }
    for _ in range(25):
        metering("succeeded")
    names = {
        "overdue_metering": ("pending",),
        "interrupted_metering": ("expired_lease",),
        "failed_metering": ("failed", "future_failed"),
    }[issue]
    assert set(
        metering_jobs_for_issue(issue, NOW).order_by("-id")[:20].values_list("pk", flat=True)
    ) == {rows[name].pk for name in names}
    assert metering_jobs_for_issue(issue, NOW).count() == delivery_health(NOW, LAG)[issue]
    assert metering_jobs_for_issue("unknown", NOW).count() == len(rows) + 25
    assert metering_jobs_for_issue("failed_collections", NOW).count() == len(rows) + 25
    assert metering_jobs_for_issue("overdue_metering", NOW, timedelta(minutes=6)).count() == 0
    Region.objects.filter(pk=plant.region_id).update(deleted_at=NOW)
    assert not metering_jobs_for_issue(issue, NOW).exists()
    assert not metering_jobs_for_issue("unknown", NOW).exists()


@pytest.mark.parametrize("issue", ["blocked_provider_credentials", "provider_cooldowns"])
def test_account_issue_filters_show_every_affected_region_and_exclude_inactive_accounts(
    issue: str,
) -> None:
    assert connection.vendor == "postgresql"
    system = make_system(approved=True)
    system.account.authentication_failed_revision = system.account.credential_revision
    system.account.request_blocked_until = NOW + BEFORE
    system.account.save()
    shared, _ = another_region(system, "Shared account")
    collection_disabled, _ = another_region(system, "Disabled collection")
    CollectionSchedule.objects.create(region=collection_disabled, enabled=False, effective_from=NOW)
    suspended, _ = another_region(system, "Suspended")
    suspended.status = "suspended"
    suspended.save(update_fields=["status"])
    deleted, _ = another_region(system, "Deleted")
    deleted.deleted_at = NOW
    deleted.save(update_fields=["deleted_at"])
    disabled_account = ProviderAccount.objects.create(
        provider="fixture",
        external_account_id="disabled",
        enabled=False,
        authentication_failed_revision=1,
        request_blocked_until=NOW + timedelta(days=1),
    )
    disabled, _ = another_region(system, "Disabled account", account=disabled_account)
    healthy_account = ProviderAccount.objects.create(
        provider="fixture",
        external_account_id="healthy",
        credential_revision=2,
        authentication_failed_revision=1,
        request_blocked_until=NOW,
    )
    healthy, _ = another_region(system, "Healthy account", account=healthy_account)
    assert set(regions_for_issue(issue, NOW).values_list("pk", flat=True)) == {
        system.region.pk,
        shared.pk,
        collection_disabled.pk,
    }
    assert delivery_health(NOW, LAG)[issue] == 1
    expected_baseline = {
        system.region.pk,
        shared.pk,
        collection_disabled.pk,
        suspended.pk,
        disabled.pk,
        healthy.pk,
    }
    assert set(regions_for_issue("unknown", NOW).values_list("pk", flat=True)) == expected_baseline
    assert (
        set(regions_for_issue("failed_notifications", NOW).values_list("pk", flat=True))
        == expected_baseline
    )


def test_unvalidated_region_filter_matches_revision_and_collection_scope() -> None:
    system = make_system(approved=True)
    system.account.credential_revision = 2
    system.account.save(update_fields=["credential_revision"])
    null_validation, binding = another_region(system, "Null validation")
    binding.validated_at = binding.validated_credential_revision = None
    binding.save(update_fields=["validated_at", "validated_credential_revision"])
    healthy, binding = another_region(system, "Validated")
    binding.validated_credential_revision = 2
    binding.save(update_fields=["validated_credential_revision"])
    disabled, _ = another_region(system, "Disabled collection")
    CollectionSchedule.objects.create(region=disabled, enabled=False, effective_from=NOW)
    pending, _ = another_region(system, "Pending")
    pending.status = "pending"
    pending.save(update_fields=["status"])
    matches = regions_for_issue("unvalidated_provider_credentials", NOW)
    assert set(matches.values_list("pk", flat=True)) == {system.region.pk, null_validation.pk}
    assert matches.count() == delivery_health(NOW, LAG)["unvalidated_provider_credentials"]
    system.account.enabled = False
    system.account.save(update_fields=["enabled"])
    assert not regions_for_issue("unvalidated_provider_credentials", NOW).exists()
    assert healthy.pk in regions_for_issue("unknown", NOW).values_list("pk", flat=True)


def saved_forecast(
    binding: SiteBinding,
    collected: datetime,
    *,
    status: str = "succeeded",
    kind: str = "forecast",
    publish: bool = True,
) -> None:
    snapshot = ForecastSnapshot.objects.create(
        binding=binding,
        job=collection(binding, status),
        collected_at=collected,
        raw_payload={},
        normalized_intervals=[],
        source_kind=kind,
    )
    if publish:
        ForecastResult.objects.create(snapshot=snapshot, payload={}, published_at=NOW)


def test_stale_region_filter_uses_successful_published_current_binding_forecast_age() -> None:
    system = make_system(approved=True)
    boundary, binding = another_region(system, "At boundary")
    saved_forecast(binding, NOW - timedelta(hours=26))
    stale, binding = another_region(system, "Before boundary")
    saved_forecast(binding, NOW - timedelta(hours=26) - BEFORE)
    failed, binding = another_region(system, "Failed acquisition")
    saved_forecast(binding, NOW, status="failed")
    unpublished, binding = another_region(system, "Unpublished")
    saved_forecast(binding, NOW, publish=False)
    historical, binding = another_region(system, "Historical observations")
    saved_forecast(binding, NOW, kind="estimated_actuals")
    disabled, _ = another_region(system, "Disabled collection")
    CollectionSchedule.objects.create(region=disabled, enabled=False, effective_from=NOW)
    latest_valid, binding = another_region(system, "Latest valid")
    saved_forecast(binding, NOW - timedelta(hours=25))
    saved_forecast(binding, NOW - timedelta(hours=27))
    matches = regions_for_issue("regions_without_recent_forecasts", NOW)
    assert set(matches.values_list("pk", flat=True)) == {
        system.region.pk,
        stale.pk,
        failed.pk,
        unpublished.pk,
        historical.pk,
    }
    assert matches.count() == delivery_health(NOW, LAG)["regions_without_recent_forecasts"]
    assert not matches.filter(pk__in=[boundary.pk, latest_valid.pk]).exists()


@pytest.mark.parametrize("recipients", [["invalid"], ["recipient@example.com", "invalid"]])
def test_invalid_recipient_filter_matches_validation_and_current_enabled_region_scope(
    recipients: list[str],
) -> None:
    system = make_system(approved=True)
    target = ReminderSchedule.objects.create(region=system.region, effective_from=NOW, enabled=True)
    store_recipients(target, recipients)
    disabled, _ = another_region(system, "Disabled schedule")
    store_recipients(
        ReminderSchedule.objects.create(region=disabled, effective_from=NOW, enabled=False),
        ["invalid"],
    )
    disabled_collection, _ = another_region(system, "Disabled collection")
    CollectionSchedule.objects.create(region=disabled_collection, enabled=False, effective_from=NOW)
    store_recipients(
        ReminderSchedule.objects.create(
            region=disabled_collection, effective_from=NOW, enabled=True
        ),
        ["invalid"],
    )
    valid, _ = another_region(system, "Valid")
    store_recipients(
        ReminderSchedule.objects.create(region=valid, effective_from=NOW, enabled=True),
        ["recipient@example.com"],
    )
    matches = regions_for_issue("invalid_reminder_recipients", NOW)
    assert set(matches.values_list("pk", flat=True)) == {system.region.pk, disabled_collection.pk}
    assert matches.count() == delivery_health(NOW, LAG)["invalid_reminder_recipients"]
    store_recipients(target, [])
    system.account.enabled = False
    system.account.save(update_fields=["enabled"])
    assert not regions_for_issue("invalid_reminder_recipients", NOW).exists()
