from datetime import datetime, timedelta

from django.db.models import Count, F, OuterRef, Q, Subquery

from pyracast.connections.models import ProviderAccount
from pyracast.forecasts.models import CollectionJob, DispatcherStatus, ForecastSnapshot
from pyracast.metering.models import EpiasCollectionJob
from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.operations import invalid_reminder_recipient_count
from pyracast.regions.models import Region


def record_dispatch_phase(phase: str, now: datetime, error: str = "") -> None:
    values: dict[str, object] = {"attempted_at": now, "last_error": error}
    if not error:
        values["succeeded_at"] = now
    DispatcherStatus.objects.update_or_create(phase=phase, defaults=values)


def delivery_health(now: datetime, maximum_lag: timedelta) -> dict[str, object]:
    cutoff = now - maximum_lag
    recent = now - timedelta(days=1)
    waiting_jobs = Q(status__in=("pending", "retry"))
    expired_leases = Q(lease_expires_at__lte=now) | Q(lease_expires_at__isnull=True)
    interrupted_jobs = Q(status="running") & expired_leases
    phases = {row.phase: row for row in DispatcherStatus.objects.all()}
    stale_phases = [
        name
        for name in DispatcherStatus.Phase.values
        if (phase := phases.get(name)) is None
        or phase.succeeded_at is None
        or phase.succeeded_at < cutoff
        or phase.last_error
    ]
    collections = CollectionJob.objects.filter(
        Q(status__in=("pending", "retry", "running"))
        | Q(status="failed", scheduled_slot__gte=recent),
        binding__region__deleted_at__isnull=True,
    )
    deliveries = NotificationDelivery.objects.filter(
        Q(status__in=("pending", "retry", "sending")) | Q(scheduled_for__gte=recent),
        schedule__region__deleted_at__isnull=True,
    )
    accounts = ProviderAccount.objects.filter(
        enabled=True,
        pk__in=Region.objects.filter(status="approved", deleted_at__isnull=True).values(
            "approved_binding__provider_account_id"
        ),
    )
    active_regions = Region.objects.filter(
        status="approved", deleted_at__isnull=True, approved_binding__provider_account__enabled=True
    ).exclude(collectionschedule__enabled=False)
    latest_forecast = (
        ForecastSnapshot.objects.filter(
            binding_id=OuterRef("approved_binding_id"),
            source_kind="forecast",
            job__status="succeeded",
            forecastresult__isnull=False,
        )
        .order_by("-collected_at")
        .values("collected_at")[:1]
    )
    stale_regions = active_regions.annotate(latest_acquisition=Subquery(latest_forecast)).filter(
        Q(latest_acquisition__isnull=True) | Q(latest_acquisition__lt=now - timedelta(hours=26))
    )
    metering = EpiasCollectionJob.objects.filter(
        Q(status__in=("pending", "retry", "running"))
        | Q(status="failed", scheduled_for__gte=recent),
        plant__deleted_at__isnull=True,
    )
    notification_counts = deliveries.aggregate(
        overdue_notifications=Count("pk", filter=waiting_jobs & Q(available_at__lt=cutoff)),
        interrupted_notifications=Count("pk", filter=Q(status="sending") & expired_leases),
        failed_notifications=Count(
            "pk",
            filter=Q(status__in=("failed", "uncertain", "unavailable"), scheduled_for__gte=recent),
        ),
        missed_notifications=Count(
            "pk",
            filter=Q(
                status__in=("skipped", "failed"),
                last_error__in=("missed_schedule", "delivery_deadline_exceeded"),
                scheduled_for__gte=recent,
            ),
        ),
        unavailable_forecasts=Count(
            "pk", filter=Q(prepared_message__kind="unavailable", scheduled_for__gte=recent)
        ),
        partial_forecasts=Count(
            "pk", filter=Q(prepared_message__tomorrow_complete=False, scheduled_for__gte=recent)
        ),
        mail_authentication_failures=Count(
            "pk", filter=Q(last_error="mail_authentication_failed", scheduled_for__gte=recent)
        ),
        saved_forecast_notifications=Count(
            "pk",
            filter=Q(
                prepared_message__kind__in=("saved_complete", "saved_partial"),
                scheduled_for__gte=recent,
            ),
        ),
    )
    saved_forecast_notifications = notification_counts.pop("saved_forecast_notifications")
    counters = {
        "invalid_reminder_recipients": invalid_reminder_recipient_count(),
        "unvalidated_provider_credentials": active_regions.exclude(
            approved_binding__validated_credential_revision=F(
                "approved_binding__provider_account__credential_revision"
            )
        ).count(),
        "regions_without_recent_forecasts": stale_regions.count(),
        **accounts.aggregate(
            provider_cooldowns=Count("pk", filter=Q(request_blocked_until__gt=now)),
            blocked_provider_credentials=Count(
                "pk", filter=Q(authentication_failed_revision=F("credential_revision"))
            ),
        ),
        **collections.aggregate(
            overdue_collections=Count("pk", filter=waiting_jobs & Q(available_at__lt=cutoff)),
            interrupted_collections=Count("pk", filter=interrupted_jobs),
            failed_collections=Count("pk", filter=Q(status="failed", scheduled_slot__gte=recent)),
        ),
        **notification_counts,
        **metering.aggregate(
            overdue_metering=Count("pk", filter=waiting_jobs & Q(next_attempt_at__lt=cutoff)),
            interrupted_metering=Count("pk", filter=interrupted_jobs),
            failed_metering=Count("pk", filter=Q(status="failed", scheduled_for__gte=recent)),
        ),
    }
    return {
        "healthy": not stale_phases and not any(counters.values()),
        "checked_at": now.isoformat(),
        "unhealthy_dispatch_phases": stale_phases,
        **counters,
        "saved_forecast_notifications": saved_forecast_notifications,
    }
