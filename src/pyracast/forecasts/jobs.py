import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from pyracast.connections.contracts import (
    Calculator,
    ProviderAdapter,
    ProviderData,
    ProviderFailure,
)
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.connections.operations import QuotaExhausted, reserve_request
from pyracast.forecasts.models import (
    CollectionJob,
    ForecastResult,
    ForecastSnapshot,
    RequestAttempt,
)
from pyracast.regions.models import Region

logger = logging.getLogger(__name__)

LEASE_DURATION = timedelta(minutes=5)


@dataclass(frozen=True)
class Lease:
    job_id: int
    binding: SiteBinding
    target_date: date
    token: uuid.UUID


def binding_is_current(binding: SiteBinding) -> bool:
    return (
        Region.objects.filter(
            pk=binding.region_id,
            status="approved",
            revision=binding.region_revision,
            approved_binding_id=binding.pk,
        ).exists()
        and binding.provider_account.enabled
        and binding.provider_account.authorized_at is not None
        and binding.validated_credential_revision == binding.provider_account.credential_revision
    )


def claim(job_id: int, now: datetime) -> Lease | None:
    started = time.monotonic()
    if now.tzinfo is None:
        raise ValidationError("An aware timestamp is required.")
    with transaction.atomic():
        job = CollectionJob.objects.select_for_update().get(pk=job_id)
        now += timedelta(seconds=time.monotonic() - started)
        if job.status in {"succeeded", "stale", "failed"} or job.available_at > now:
            return None
        if (
            job.status == "running"
            and job.lease_expires_at is not None
            and (job.lease_expires_at > now)
        ):
            return None
        binding = SiteBinding.objects.select_related("provider_account").get(pk=job.binding_id)
        if not binding_is_current(binding):
            job.status = "stale"
            job.save(update_fields=("status",))
            return None
        token = uuid.uuid4()
        job.status = "running"
        job.lease_token = token
        job.lease_expires_at = now + LEASE_DURATION
        job.attempts += 1
        job.save(update_fields=("status", "lease_token", "lease_expires_at", "attempts"))
        return Lease(job.pk, binding, job.target_date, token)


def retry(lease: Lease, now: datetime, code: str, *, quota: bool = False) -> None:
    with transaction.atomic():
        job = CollectionJob.objects.select_for_update().get(pk=lease.job_id)
        if job.lease_token != lease.token or job.status != "running":
            return
        job.last_error = code
        job.status = "retry" if quota or job.attempts < 3 else "failed"
        if quota:
            tomorrow = now.astimezone(UTC).date() + timedelta(days=1)
            job.available_at = datetime.combine(tomorrow, datetime.min.time(), tzinfo=UTC)
        else:
            job.available_at = now + timedelta(seconds=30 * 2 ** min(job.attempts, 6))
        job.lease_token = None
        job.lease_expires_at = None
        job.save()


def persist_snapshot(lease: Lease, data: ProviderData, now: datetime) -> ForecastSnapshot | None:
    started = time.monotonic()
    with transaction.atomic():
        job = CollectionJob.objects.select_for_update().get(pk=lease.job_id)
        now += timedelta(seconds=time.monotonic() - started)
        if (
            job.lease_token != lease.token
            or job.lease_expires_at is None
            or (job.lease_expires_at <= now)
        ):
            return None
        snapshot, _ = ForecastSnapshot.objects.get_or_create(
            job=job,
            defaults={
                "raw_payload": data.raw_payload,
                "normalized_intervals": data.intervals,
                "collected_at": data.collected_at or now,
                "issued_at": data.issued_at,
                "source_kind": data.source_kind,
            },
        )
        return snapshot


def publish(
    lease: Lease, snapshot: ForecastSnapshot, payload: dict[str, object], now: datetime
) -> bool:
    started = time.monotonic()
    with transaction.atomic():
        Region.objects.select_for_update().get(pk=lease.binding.region_id)
        job = CollectionJob.objects.select_for_update().get(pk=lease.job_id)
        if snapshot.job_id != job.pk or job.binding_id != lease.binding.pk:
            raise ValidationError("Snapshot, binding and lease must belong to the same job.")
        snapshot.refresh_from_db()
        if snapshot.source_kind != "forecast":
            raise ValidationError("Only forecast snapshots can be published.")
        ProviderAccount.objects.select_for_update().get(pk=lease.binding.provider_account_id)
        now += timedelta(seconds=time.monotonic() - started)
        if (
            job.lease_token != lease.token
            or job.lease_expires_at is None
            or (job.lease_expires_at <= now)
        ):
            return False
        binding = SiteBinding.objects.select_related("provider_account").get(pk=job.binding_id)
        if not binding_is_current(binding):
            job.status = "stale"
        else:
            ForecastResult.objects.get_or_create(
                snapshot=snapshot, defaults={"payload": payload, "published_at": now}
            )
            job.status = "succeeded"
        job.lease_token = None
        job.lease_expires_at = None
        job.save(update_fields=("status", "lease_token", "lease_expires_at"))
        return job.status == "succeeded"


def collect(job_id: int, adapter: ProviderAdapter, calculator: Calculator, now: datetime) -> bool:
    started = time.monotonic()
    lease = claim(job_id, now)
    if lease is None:
        return False
    snapshot = ForecastSnapshot.objects.filter(job_id=job_id).first()
    if snapshot is None:
        try:
            job = CollectionJob.objects.get(pk=job_id)
            attempt = reserve_request(
                lease.binding,
                now + timedelta(seconds=time.monotonic() - started),
                purpose="forecast",
                job=job,
            )
        except QuotaExhausted:
            retry(
                lease,
                now + timedelta(seconds=time.monotonic() - started),
                "quota_exhausted",
                quota=True,
            )
            return False
        try:
            data = adapter.fetch(lease.binding, lease.target_date)
        except ProviderFailure:
            RequestAttempt.objects.filter(pk=attempt.pk).update(outcome="failed")
            logger.warning("Provider request failed correlation_id=%s", attempt.correlation_id)
            retry(lease, now + timedelta(seconds=time.monotonic() - started), "provider_failure")
            return False
        RequestAttempt.objects.filter(pk=attempt.pk).update(outcome="succeeded")
        logger.info("Provider request succeeded correlation_id=%s", attempt.correlation_id)
        snapshot = persist_snapshot(
            lease, data, now + timedelta(seconds=time.monotonic() - started)
        )
        if snapshot is None:
            return False
    if snapshot.source_kind != "forecast":
        retry(lease, now + timedelta(seconds=time.monotonic() - started), "invalid_source_kind")
        return False
    data = ProviderData(
        snapshot.raw_payload,
        snapshot.normalized_intervals,
        snapshot.issued_at,
        snapshot.collected_at,
        source_kind="forecast",
    )
    try:
        payload = calculator(lease.binding, lease.target_date, data)
    except ValueError, ArithmeticError:
        retry(lease, now + timedelta(seconds=time.monotonic() - started), "calculation_failure")
        return False
    return publish(lease, snapshot, payload, now + timedelta(seconds=time.monotonic() - started))


def pending_job_ids(now: datetime, limit: int = 100) -> list[int]:
    return list(
        CollectionJob.objects.filter(available_at__lte=now)
        .filter(Q(status__in=("pending", "retry")) | Q(status="running", lease_expires_at__lte=now))
        .order_by("available_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
