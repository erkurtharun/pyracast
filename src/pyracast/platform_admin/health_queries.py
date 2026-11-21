from datetime import datetime, timedelta

from django.core.exceptions import ValidationError
from django.db.models import F, OuterRef, Q, QuerySet, Subquery

from pyracast.forecasts.models import CollectionJob, CollectionJobStatus, ForecastSnapshot
from pyracast.mail.errors import CREDENTIAL_FAILURES
from pyracast.metering.models import EpiasCollectionJob, EpiasJobStatus
from pyracast.notifications.models import (
    PAUSED_DELIVERY_ERRORS,
    NotificationDelivery,
    NotificationDeliveryError,
    NotificationDeliveryStatus,
    PreparedMessageKind,
    ReminderSchedule,
)
from pyracast.notifications.operations import effective_recipients, validate_recipient
from pyracast.plants.models import active_plant
from pyracast.regions.models import Region, active_region


def collection_jobs_for_issue(
    issue: str, now: datetime, maximum_lag: timedelta = timedelta(minutes=5)
) -> QuerySet[CollectionJob]:
    jobs = CollectionJob.objects.filter(active_region("binding__region__"))
    if issue == "overdue_collections":
        return jobs.filter(
            status__in=(CollectionJobStatus.PENDING, CollectionJobStatus.RETRY),
            available_at__lt=now - maximum_lag,
        )
    if issue == "interrupted_collections":
        return jobs.filter(
            lease_expires_at__lte=now,
            status=CollectionJobStatus.RUNNING,
        )
    if issue == "failed_collections":
        return jobs.filter(
            status=CollectionJobStatus.FAILED, scheduled_for__gte=now - timedelta(days=1)
        )
    return jobs


def notification_deliveries_for_issue(
    issue: str, now: datetime, maximum_lag: timedelta = timedelta(minutes=5)
) -> QuerySet[NotificationDelivery]:
    deliveries = NotificationDelivery.objects.defer("prepared_message").filter(
        active_region("schedule__region__")
    )
    recent = now - timedelta(days=1)
    if issue == "overdue_notifications":
        return deliveries.filter(
            status__in=(NotificationDeliveryStatus.PENDING, NotificationDeliveryStatus.RETRY),
            available_at__lt=now - maximum_lag,
        )
    if issue == "interrupted_notifications":
        return deliveries.filter(
            lease_expires_at__lte=now,
            status=NotificationDeliveryStatus.SENDING,
        )
    if issue == "failed_notifications":
        return deliveries.filter(
            status__in=(NotificationDeliveryStatus.FAILED, NotificationDeliveryStatus.UNCERTAIN),
            scheduled_for__gte=recent,
        )
    if issue == "missed_notifications":
        return deliveries.filter(
            status__in=(NotificationDeliveryStatus.SKIPPED, NotificationDeliveryStatus.FAILED),
            last_error__in=(
                NotificationDeliveryError.MISSED_SCHEDULE,
                NotificationDeliveryError.DELIVERY_DEADLINE_EXCEEDED,
            ),
            scheduled_for__gte=recent,
        )
    if issue == "unavailable_forecasts":
        return deliveries.filter(
            forecast_kind=PreparedMessageKind.UNAVAILABLE, scheduled_for__gte=recent
        )
    if issue == "partial_forecasts":
        return deliveries.filter(forecast_complete=False, scheduled_for__gte=recent)
    if issue == "mail_authentication_failures":
        return deliveries.filter(last_error__in=CREDENTIAL_FAILURES, scheduled_for__gte=recent)
    if issue == "paused_notifications":
        return deliveries.filter(
            status=NotificationDeliveryStatus.SKIPPED,
            last_error__in=PAUSED_DELIVERY_ERRORS,
            scheduled_for__gte=recent,
        )
    return deliveries


def metering_jobs_for_issue(
    issue: str, now: datetime, maximum_lag: timedelta = timedelta(minutes=5)
) -> QuerySet[EpiasCollectionJob]:
    jobs = EpiasCollectionJob.objects.filter(active_plant("plant__"))
    if issue == "overdue_metering":
        return jobs.filter(status=EpiasJobStatus.PENDING, available_at__lt=now - maximum_lag)
    if issue == "interrupted_metering":
        return jobs.filter(
            lease_expires_at__lte=now,
            status=EpiasJobStatus.RUNNING,
        )
    if issue == "failed_metering":
        return jobs.filter(status=EpiasJobStatus.FAILED, scheduled_for__gte=now - timedelta(days=1))
    return jobs


def regions_for_issue(issue: str, now: datetime) -> QuerySet[Region]:
    regions = Region.objects.active()
    if issue == "invalid_reminder_recipients":
        invalid_region_ids: list[int] = []
        for schedule in ReminderSchedule.objects.filter(
            active_region("region__"),
            enabled=True,
            region__status="approved",
            region__approved_binding__provider_account__enabled=True,
        ):
            try:
                for address in effective_recipients(schedule):
                    validate_recipient(address)
            except ValidationError:
                invalid_region_ids.append(schedule.region_id)
        return regions.filter(pk__in=invalid_region_ids)
    approved = regions.filter(status="approved", approved_binding__provider_account__enabled=True)
    if issue == "provider_cooldowns":
        return approved.filter(approved_binding__provider_account__request_blocked_until__gt=now)
    if issue == "blocked_provider_credentials":
        return approved.filter(
            approved_binding__provider_account__authentication_failed_revision=F(
                "approved_binding__provider_account__credential_revision"
            )
        )
    active = approved.exclude(collectionschedule__enabled=False)
    if issue == "unvalidated_provider_credentials":
        return active.exclude(
            approved_binding__validated_credential_revision=F(
                "approved_binding__provider_account__credential_revision"
            )
        )
    if issue == "regions_without_recent_forecasts":
        latest_forecast = (
            ForecastSnapshot.objects.filter(
                binding_id=OuterRef("approved_binding_id"),
                source_kind="forecast",
                job__status=CollectionJobStatus.SUCCEEDED,
                forecastresult__isnull=False,
            )
            .order_by("-collected_at")
            .values("collected_at")[:1]
        )
        return active.annotate(latest_acquisition=Subquery(latest_forecast)).filter(
            Q(latest_acquisition__isnull=True) | Q(latest_acquisition__lt=now - timedelta(hours=26))
        )
    return regions
