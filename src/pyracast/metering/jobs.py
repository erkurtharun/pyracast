import hashlib
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from pyracast.epias import EpiasDay, EpiasError, EpiasPlant
from pyracast.metering.client import get_epias_client
from pyracast.metering.models import EpiasCollectionJob, EpiasConnection, EpiasSnapshot
from pyracast.plants.models import Plant
from pyracast.plants.operations import configuration_is_current
from pyracast.regions.models import Region

Fetcher = Callable[[EpiasPlant, date], EpiasDay]
LEASE_DURATION = timedelta(minutes=5)


@dataclass(frozen=True)
class EpiasLease:
    job: EpiasCollectionJob
    token: uuid.UUID


def _locked_job(job_id: int) -> EpiasCollectionJob | None:
    hint = (
        EpiasCollectionJob.objects.filter(pk=job_id)
        .values("connection__plant__region_id", "connection__plant_id", "connection_id")
        .first()
    )
    if hint is None:
        return None
    if (
        not Region.objects.select_for_update()
        .filter(pk=hint["connection__plant__region_id"])
        .exists()
    ):
        return None
    if not Plant.objects.select_for_update().filter(pk=hint["connection__plant_id"]).exists():
        return None
    if not EpiasConnection.objects.select_for_update().filter(pk=hint["connection_id"]).exists():
        return None
    return EpiasCollectionJob.objects.select_for_update().filter(pk=job_id).first()


def _current(job: EpiasCollectionJob) -> bool:
    connection = job.connection
    return (
        connection.enabled
        and connection.revision == job.connection_revision
        and connection.plant.current_configuration_id == job.configuration_id
        and configuration_is_current(job.configuration)
    )


def claim_epias_job(job_id: int, now: datetime) -> EpiasLease | None:
    started = time.monotonic()
    if now.tzinfo is None:
        raise ValidationError("An aware timestamp is required.")
    with transaction.atomic():
        job = _locked_job(job_id)
        now += timedelta(seconds=time.monotonic() - started)
        if job is None or job.status not in {"pending", "running"} or job.next_attempt_at > now:
            return None
        if job.status == "running" and job.lease_expires_at and job.lease_expires_at > now:
            return None
        if not _current(job):
            job.status = "superseded"
        elif job.attempts >= 3:
            job.status = "failed"
            job.last_error = "attempts_exhausted"
        else:
            job.status = "running"
            job.attempts += 1
            job.lease_token = uuid.uuid4()
            job.lease_expires_at = now + LEASE_DURATION
            job.save()
            return EpiasLease(job, job.lease_token)
        job.lease_token = None
        job.lease_expires_at = None
        job.save()
        return None


def _owns(job: EpiasCollectionJob, lease: EpiasLease, now: datetime) -> bool:
    return (
        job.status == "running"
        and job.lease_token == lease.token
        and job.lease_expires_at is not None
        and job.lease_expires_at > now
    )


def _finish_failure(lease: EpiasLease, now: datetime, retry_after: int | None) -> None:
    started = time.monotonic()
    with transaction.atomic():
        job = _locked_job(lease.job.pk)
        now += timedelta(seconds=time.monotonic() - started)
        if job is None or not _owns(job, lease, now):
            return
        job.status = "pending" if job.attempts < 3 else "failed"
        if not _current(job):
            job.status = "superseded"
        job.last_error = "epias_provider_failure"
        delay = max(30 * 2**job.attempts, min(retry_after or 0, 86400))
        job.next_attempt_at = now + timedelta(seconds=delay)
        job.lease_token = None
        job.lease_expires_at = None
        job.save()


def _publish(lease: EpiasLease, data: EpiasDay, now: datetime) -> bool:
    started = time.monotonic()
    raw = json.loads(data.raw_payload)
    readings = [
        {
            "provider_timestamp": row.timestamp.isoformat(),
            "hour": row.hour,
            "total_mwh": str(row.total) if row.total is not None else None,
            "sun_mwh": str(row.sun) if row.sun is not None else None,
        }
        for row in data.readings
    ]
    digest = hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    complete = {row.hour for row in data.readings} == {
        f"{hour:02}:00" for hour in range(24)
    } and all(row.total is not None for row in data.readings)
    with transaction.atomic():
        job = _locked_job(lease.job.pk)
        now += timedelta(seconds=time.monotonic() - started)
        if job is None or not _owns(job, lease, now):
            return False
        if not _current(job):
            job.status = "superseded"
        else:
            EpiasSnapshot.objects.get_or_create(
                job=job,
                attempt=job.attempts,
                defaults={
                    "connection_id": job.connection_id,
                    "configuration_id": job.configuration_id,
                    "target_day": job.target_day,
                    "collected_at": data.collected_at,
                    "available_at": now,
                    "raw_payload": raw,
                    "readings": readings,
                    "content_sha256": digest,
                    "complete": complete,
                },
            )
            job.status = "succeeded" if complete else "pending" if job.attempts < 3 else "failed"
            job.last_error = "" if complete else "partial_data"
            if not complete:
                job.next_attempt_at = now + timedelta(minutes=30 * 2 ** (job.attempts - 1))
        job.lease_token = None
        job.lease_expires_at = None
        job.save()
        return job.status == "succeeded"


def run_epias_job(
    job_id: int, *, fetch: Fetcher | None = None, now: datetime | None = None
) -> bool:
    now = now or timezone.now()
    started = time.monotonic()
    lease = claim_epias_job(job_id, now)
    if lease is None:
        return False
    try:
        plant = EpiasPlant(**lease.job.provider_plant)
        data = (fetch or get_epias_client().fetch_day)(plant, lease.job.target_day)
        if data.plant.id != plant.id or data.day != lease.job.target_day:
            raise EpiasError("epias_response_identity_mismatch")
        return _publish(lease, data, now + timedelta(seconds=time.monotonic() - started))
    except Exception as exc:
        retry_after = exc.retry_after_seconds if isinstance(exc, EpiasError) else None
        _finish_failure(lease, now + timedelta(seconds=time.monotonic() - started), retry_after)
        return False


def pending_epias_ids(now: datetime, *, limit: int = 100) -> list[int]:
    return list(
        EpiasCollectionJob.objects.filter(
            Q(status="pending") | Q(status="running", lease_expires_at__lte=now),
            next_attempt_at__lte=now,
        )
        .order_by("next_attempt_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
