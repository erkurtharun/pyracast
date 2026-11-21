from datetime import datetime
from typing import Protocol

from django.core.exceptions import ValidationError
from django.db import transaction

from pyracast.background.clock import Clock
from pyracast.background.lease import owns, release
from pyracast.collection_window import NIGHT_RETRY_DELAY
from pyracast.connections.currency import binding_is_current
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.acquisition import missing_actuals_window
from pyracast.forecasts.jobs.claim import Lease
from pyracast.forecasts.models import (
    CollectionEndpoint,
    CollectionJob,
    CollectionJobError,
    CollectionJobStatus,
    CollectionSchedule,
    ForecastResult,
    ForecastSnapshot,
    SnapshotSourceKind,
)
from pyracast.regions.models import Region


class IssuanceArchive[Prepared](Protocol):
    def save(
        self, prepared: Prepared, snapshot: ForecastSnapshot, published_at: datetime
    ) -> None: ...


class PublicationHooks[Prepared](Protocol):
    """What a publication does for the plants of its region; the publisher owns the transaction."""

    def prepare(self, snapshot: ForecastSnapshot) -> Prepared: ...

    # Called inside the transaction after the job lock and before the provider account lock.
    def lock(self, region_id: int) -> IssuanceArchive[Prepared]: ...

    def actuals_published(self, binding: SiteBinding, now: datetime) -> None: ...


def publish[Prepared](
    lease: Lease,
    snapshot: ForecastSnapshot,
    payload: dict[str, object],
    now: datetime,
    *,
    hooks: PublicationHooks[Prepared],
    daily_payloads: dict[str, dict[str, object]] | None = None,
    prepared_issuances: Prepared | None = None,
) -> bool:
    clock = Clock(now)
    if prepared_issuances is None:
        prepared_issuances = hooks.prepare(snapshot)
    with transaction.atomic():
        Region.objects.select_for_update().get(pk=lease.binding.region_id)
        job = CollectionJob.objects.select_for_update().get(pk=lease.job_id)
        if snapshot.job_id != job.pk or job.binding_id != lease.binding.pk:
            raise ValidationError("Snapshot, binding and lease must belong to the same job.")
        snapshot.refresh_from_db()
        expected_kind = (
            SnapshotSourceKind.ESTIMATED_ACTUALS
            if job.endpoint == CollectionEndpoint.ESTIMATED_ACTUALS
            else SnapshotSourceKind.FORECAST
        )
        if snapshot.source_kind != expected_kind:
            raise ValidationError("Snapshot source must match the collection endpoint.")
        archive = hooks.lock(lease.binding.region_id)
        ProviderAccount.objects.select_for_update().get(pk=lease.binding.provider_account_id)
        now = clock.now()
        if not owns(job, lease.token, CollectionJobStatus.RUNNING, now):
            return False
        binding = SiteBinding.objects.select_related("provider_account").get(pk=job.binding_id)
        if not binding_is_current(binding):
            job.status = CollectionJobStatus.STALE
            job.last_error = CollectionJobError.BINDING_NOT_CURRENT
        else:
            if job.endpoint == CollectionEndpoint.FORECASTS:
                ForecastResult.objects.get_or_create(
                    snapshot=snapshot,
                    defaults={
                        "payload": payload,
                        "published_at": now,
                        "daily_payloads": daily_payloads or {},
                    },
                )
                archive.save(prepared_issuances, snapshot, now)
            ForecastSnapshot.objects.filter(pk=snapshot.pk, available_at__isnull=True).update(
                available_at=now
            )
            job.status = CollectionJobStatus.SUCCEEDED
            job.last_error = ""
            if (
                job.endpoint == CollectionEndpoint.ESTIMATED_ACTUALS
                and job.retry_until is not None
                and missing_actuals_window(binding, now, acquired=snapshot) is not None
            ):
                job.last_error = CollectionJobError.HISTORY_INCOMPLETE
                available = now + NIGHT_RETRY_DELAY
                schedule = CollectionSchedule.objects.filter(region_id=binding.region_id).first()
                if available < job.retry_until:
                    CollectionJob.objects.get_or_create(
                        binding=binding,
                        endpoint=CollectionEndpoint.ESTIMATED_ACTUALS,
                        scheduled_for=available,
                        defaults={
                            "target_date": job.target_date,
                            "available_at": available,
                            "retry_until": job.retry_until,
                            "status": CollectionJobStatus.PENDING
                            if schedule is None or schedule.enabled
                            else CollectionJobStatus.STALE,
                            "last_error": ""
                            if schedule is None or schedule.enabled
                            else CollectionJobError.SCHEDULE_DISABLED,
                        },
                    )
        release(job)
        job.save(update_fields=("status", "last_error", "lease_token", "lease_expires_at"))
        if (
            job.status == CollectionJobStatus.SUCCEEDED
            and job.endpoint == CollectionEndpoint.ESTIMATED_ACTUALS
        ):
            hooks.actuals_published(binding, now)
        return job.status == CollectionJobStatus.SUCCEEDED
