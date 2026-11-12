from datetime import datetime, timedelta

from django.db.models import F, Q

from pyracast.connections.models import ProviderAccount
from pyracast.forecasts.models import CollectionJob, DispatcherStatus
from pyracast.notifications.models import NotificationDelivery
from pyracast.regions.models import Region


def record_dispatch_phase(phase: str, now: datetime, error: str = "") -> None:
    values: dict[str, object] = {"attempted_at": now, "last_error": error}
    if not error:
        values["succeeded_at"] = now
    DispatcherStatus.objects.update_or_create(phase=phase, defaults=values)


def delivery_health(now: datetime, maximum_lag: timedelta) -> dict[str, object]:
    cutoff = now - maximum_lag
    recent = now - timedelta(days=1)
    phases = {row.phase: row for row in DispatcherStatus.objects.all()}
    stale_phases = [
        name
        for name in DispatcherStatus.Phase.values
        if (phase := phases.get(name)) is None
        or phase.succeeded_at is None
        or phase.succeeded_at < cutoff
        or phase.last_error
    ]
    collections = CollectionJob.objects.filter(binding__region__deleted_at__isnull=True)
    deliveries = NotificationDelivery.objects.filter(preference__region__deleted_at__isnull=True)
    accounts = ProviderAccount.objects.filter(
        enabled=True,
        pk__in=Region.objects.filter(status="approved", deleted_at__isnull=True).values(
            "approved_binding__provider_account_id"
        ),
    )
    counters = {
        "overdue_collections": collections.filter(
            status__in=("pending", "retry"), available_at__lt=cutoff
        ).count(),
        "interrupted_collections": collections.filter(
            Q(lease_expires_at__lte=now) | Q(lease_expires_at__isnull=True), status="running"
        ).count(),
        "failed_collections": collections.filter(
            status="failed", scheduled_slot__gte=recent
        ).count(),
        "overdue_notifications": deliveries.filter(
            status__in=("pending", "retry"), available_at__lt=cutoff
        ).count(),
        "interrupted_notifications": deliveries.filter(
            Q(lease_expires_at__lte=now) | Q(lease_expires_at__isnull=True), status="sending"
        ).count(),
        "failed_notifications": deliveries.filter(
            status__in=("failed", "uncertain", "unavailable"), scheduled_for__gte=recent
        ).count(),
        "unavailable_forecasts": deliveries.filter(
            prepared_message__kind="unavailable", scheduled_for__gte=recent
        ).count(),
        "partial_forecasts": deliveries.filter(
            prepared_message__tomorrow_complete=False, scheduled_for__gte=recent
        ).count(),
        "mail_authentication_failures": deliveries.filter(
            last_error="mail_authentication_failed", scheduled_for__gte=recent
        ).count(),
        "blocked_provider_credentials": accounts.filter(
            authentication_failed_revision=F("credential_revision")
        ).count(),
    }
    return {
        "healthy": not stale_phases and not any(counters.values()),
        "checked_at": now.isoformat(),
        "unhealthy_dispatch_phases": stale_phases,
        **counters,
        "saved_forecast_notifications": deliveries.filter(
            prepared_message__kind__in=("saved_complete", "saved_partial"),
            scheduled_for__gte=recent,
        ).count(),
        "provider_cooldowns": accounts.filter(request_blocked_until__gt=now).count(),
    }
