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

from pyracast.collection_window import NIGHT_RETRY_DELAY
from pyracast.epias import TIMEZONE, EpiasDay, EpiasError, EpiasPlant
from pyracast.forecasts.models import CollectionSchedule
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
        and job.source_kind in {"realtime_generation", "settlement_injection"}
        and job.provider_plant.get("eic") == connection.eic
        and job.provider_plant.get("id")
        == (
            connection.settlement_plant_id
            if job.source_kind == "settlement_injection"
            else connection.provider_plant_id
        )
        and connection.plant.current_configuration_id == job.configuration_id
        and configuration_is_current(job.configuration)
        and (
            job.retry_until is None
            or not CollectionSchedule.objects.filter(
                region_id=connection.plant.region_id, enabled=False
            ).exists()
        )
    )


def _nightly_retry(job: EpiasCollectionJob) -> bool:
    return (
        job.retry_until is not None
        and job.source_kind == "realtime_generation"
        and job.target_day == job.scheduled_for.astimezone(TIMEZONE).date() - timedelta(days=1)
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
        elif job.retry_until is not None and now >= job.retry_until:
            job.status = "failed"
            job.last_error = "retry_window_expired"
        elif not _nightly_retry(job) and job.attempts >= 3:
            job.status = "failed"
            job.last_error = "attempts_exhausted"
        else:
            job.status = "running"
            job.attempts += 1
            job.lease_token = uuid.uuid4()
            job.lease_expires_at = now + LEASE_DURATION
            if job.retry_until is not None:
                job.next_attempt_at = now + NIGHT_RETRY_DELAY
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


def _finish_failure(
    lease: EpiasLease,
    now: datetime,
    retry_after: int | None,
    *,
    error: str = "epias_provider_failure",
) -> None:
    started = time.monotonic()
    with transaction.atomic():
        job = _locked_job(lease.job.pk)
        now += timedelta(seconds=time.monotonic() - started)
        if job is None or not _owns(job, lease, now):
            return
        job.last_error = error
        if job.retry_until is not None:
            delay = max(NIGHT_RETRY_DELAY, timedelta(seconds=max(retry_after or 0, 0)))
            _schedule_retry(job, now, delay)
        else:
            job.status = "pending" if job.attempts < 3 else "failed"
            delay_seconds = max(30 * 2**job.attempts, min(retry_after or 0, 86400))
            job.next_attempt_at = now + timedelta(seconds=delay_seconds)
        if not _current(job):
            job.status = "superseded"
        job.lease_token = None
        job.lease_expires_at = None
        job.save()


def _schedule_retry(job: EpiasCollectionJob, now: datetime, delay: timedelta) -> None:
    job.next_attempt_at = now + delay
    job.status = "pending"
    if (job.retry_until is not None and job.next_attempt_at >= job.retry_until) or (
        not _nightly_retry(job) and job.attempts >= 3
    ):
        job.status = "failed"


def _publish(lease: EpiasLease, data: EpiasDay, now: datetime) -> bool:
    started = time.monotonic()
    raw = json.loads(data.raw_payload)
    end_day = lease.job.period_end_day or lease.job.target_day
    daily: list[tuple[date, dict[str, object], list[dict[str, object]], bool]] = []
    for offset in range((end_day - lease.job.target_day).days + 1):
        target_day = lease.job.target_day + timedelta(days=offset)
        rows = [row for row in data.readings if row.timestamp.date() == target_day]
        day_raw = raw
        if lease.job.source_kind == "settlement_injection":
            day_raw = {
                "items": [
                    item
                    for item in raw["items"]
                    if datetime.fromisoformat(item["date"]).astimezone(TIMEZONE).date()
                    == target_day
                ],
                "request_period": {"start_day": str(data.day), "end_day": str(end_day)},
            }
        readings: list[dict[str, object]] = [
            {
                "provider_timestamp": row.timestamp.isoformat(),
                "hour": row.hour,
                "period_start": row.period_start.isoformat(),
                "period_end": row.period_end.isoformat(),
                "total_mwh": str(row.total) if row.total is not None else None,
                "sun_mwh": str(row.sun) if row.sun is not None else None,
            }
            for row in rows
        ]
        complete = (
            len(rows) == 24
            and {row.hour for row in rows} == {f"{hour:02}:00" for hour in range(24)}
            and all(row.total is not None for row in rows)
        )
        daily.append((target_day, day_raw, readings, complete))
    complete = all(item[3] for item in daily)
    with transaction.atomic():
        job = _locked_job(lease.job.pk)
        now += timedelta(seconds=time.monotonic() - started)
        if job is None or not _owns(job, lease, now):
            return False
        if not _current(job):
            job.status = "superseded"
        else:
            for target_day, day_raw, readings, day_complete in daily:
                digest = hashlib.sha256(
                    json.dumps(day_raw, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                EpiasSnapshot.objects.get_or_create(
                    job=job,
                    attempt=job.attempts,
                    target_day=target_day,
                    defaults={
                        "connection_id": job.connection_id,
                        "configuration_id": job.configuration_id,
                        "source_kind": job.source_kind,
                        "provider_plant": job.provider_plant,
                        "endpoint": data.endpoint,
                        "collected_at": data.collected_at,
                        "available_at": now,
                        "raw_payload": day_raw,
                        "readings": readings,
                        "content_sha256": digest,
                        "complete": day_complete,
                    },
                )
            if complete:
                job.status = "succeeded"
                job.last_error = ""
            elif job.source_kind == "settlement_injection":
                job.status = "awaiting_publication"
                job.last_error = "unpublished_data" if not data.readings else "partial_data"
            else:
                job.last_error = "partial_data"
                if job.retry_until is not None:
                    _schedule_retry(job, now, NIGHT_RETRY_DELAY)
                else:
                    job.status = "pending" if job.attempts < 3 else "failed"
                    job.next_attempt_at = now + timedelta(minutes=30 * 2 ** (job.attempts - 1))
        job.lease_token = None
        job.lease_expires_at = None
        job.save()
        if job.status != "superseded":
            from pyracast.plants.preparation import prepare_calibration

            prepare_calibration(job.connection.plant_id)
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
        request_at = now + timedelta(seconds=time.monotonic() - started)
        if lease.job.retry_until is not None and request_at >= lease.job.retry_until:
            _finish_failure(lease, request_at, None, error="retry_window_expired")
            return False
        plant = EpiasPlant(**lease.job.provider_plant)
        if fetch is not None:
            data = fetch(plant, lease.job.target_day)
        elif lease.job.source_kind == "settlement_injection":
            data = get_epias_client().fetch_settlement(
                plant, lease.job.target_day, lease.job.period_end_day or lease.job.target_day
            )
        else:
            data = get_epias_client().fetch_day(plant, lease.job.target_day)
        if (
            data.plant.id != plant.id
            or data.plant.eic != plant.eic
            or data.day != lease.job.target_day
            or data.source_kind != lease.job.source_kind
            or data.end_day != lease.job.period_end_day
        ):
            raise EpiasError("epias_response_identity_mismatch")
        return _publish(lease, data, now + timedelta(seconds=time.monotonic() - started))
    except Exception as exc:
        retry_after = exc.retry_after_seconds if isinstance(exc, EpiasError) else None
        _finish_failure(lease, now + timedelta(seconds=time.monotonic() - started), retry_after)
        return False


def pending_epias_ids(now: datetime, *, limit: int = 100) -> list[int]:
    eligible = Q(status="pending") | Q(status="running", lease_expires_at__lte=now)
    EpiasCollectionJob.objects.filter(eligible, retry_until__lte=now).update(
        status="failed", last_error="retry_window_expired", lease_token=None, lease_expires_at=None
    )
    return list(
        EpiasCollectionJob.objects.filter(
            eligible,
            next_attempt_at__lte=now,
        )
        .order_by("next_attempt_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
