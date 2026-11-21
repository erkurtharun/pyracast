import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from django.db import transaction
from django.db.models import Case, F, Q, Value, When
from django.utils import timezone

from pyracast.background.clock import Clock
from pyracast.background.deadlines import ACTORS, LAST_REQUEST_START
from pyracast.background.failures import BACK_OFF, log_failure
from pyracast.background.lease import RELEASED, claimable, held, owns, release, take
from pyracast.background.retry_policy import PROVIDER_JOB
from pyracast.collection_window import NIGHT_RETRY_DELAY
from pyracast.epias import TIMEZONE, EpiasCredentialsError, EpiasDay, EpiasError, EpiasPlant
from pyracast.forecasts.models import CollectionSchedule
from pyracast.metering.client import get_epias_client
from pyracast.metering.models import (
    EpiasCollectionJob,
    EpiasConnection,
    EpiasJobError,
    EpiasJobStatus,
    EpiasSnapshot,
    EpiasSourceKind,
)
from pyracast.plants.models import Plant
from pyracast.plants.operations import configuration_is_current
from pyracast.provider_retry import next_retry_at
from pyracast.regions.models import Region

Fetcher = Callable[[EpiasPlant, date], EpiasDay]
LEASE_DURATION = ACTORS["collect_epias"].lease
# A job awaiting publication waits for EPIAS, not for a worker.
WAITING = (EpiasJobStatus.PENDING,)
CREDENTIAL_ERRORS = (EpiasJobError.CREDENTIALS_MISSING, EpiasJobError.CREDENTIALS_REJECTED)

logger = logging.getLogger(__name__)


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
    Plant.objects.select_for_update().values_list("pk", flat=True).get(
        pk=hint["connection__plant_id"]
    )
    if not EpiasConnection.objects.select_for_update().filter(pk=hint["connection_id"]).exists():
        return None
    return EpiasCollectionJob.objects.select_for_update().filter(pk=job_id).first()


def _current(job: EpiasCollectionJob) -> bool:
    connection = job.connection
    return (
        connection.enabled
        and connection.revision == job.connection_revision
        and job.source_kind in EpiasSourceKind.values
        and job.provider_plant.get("eic") == connection.eic
        and job.provider_plant.get("id")
        == (
            connection.settlement_plant_id
            if job.source_kind == EpiasSourceKind.SETTLEMENT_INJECTION
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
        and job.source_kind == EpiasSourceKind.REALTIME_GENERATION
        and job.target_date == job.scheduled_for.astimezone(TIMEZONE).date() - timedelta(days=1)
    )


def claim_epias_job(job_id: int, now: datetime) -> EpiasLease | None:
    clock = Clock(now)
    with transaction.atomic():
        job = _locked_job(job_id)
        now = clock.now()
        if (
            job is None
            or job.status not in {EpiasJobStatus.PENDING, EpiasJobStatus.RUNNING}
            or job.available_at > now
        ):
            return None
        if held(job, EpiasJobStatus.RUNNING, now):
            return None
        if not _current(job):
            job.status = EpiasJobStatus.SUPERSEDED
        elif job.retry_until is not None and now >= job.retry_until:
            job.status = EpiasJobStatus.FAILED
            if job.last_error not in CREDENTIAL_ERRORS:
                job.last_error = EpiasJobError.RETRY_WINDOW_EXPIRED
        elif not _nightly_retry(job) and PROVIDER_JOB.exhausted(job.attempts):
            job.status = EpiasJobStatus.FAILED
            job.last_error = EpiasJobError.ATTEMPTS_EXHAUSTED
        else:
            token = take(job, EpiasJobStatus.RUNNING, now, LEASE_DURATION)
            if job.retry_until is not None:
                job.available_at = now + NIGHT_RETRY_DELAY
            job.save()
            return EpiasLease(job, token)
        release(job)
        job.save()
        return None


def _owns(job: EpiasCollectionJob, lease: EpiasLease, now: datetime) -> bool:
    return owns(job, lease.token, EpiasJobStatus.RUNNING, now)


def _finish_failure(
    lease: EpiasLease,
    now: datetime,
    retry_after: int | None,
    *,
    error: EpiasJobError = EpiasJobError.PROVIDER_FAILURE,
    retryable: bool = True,
    waiting: bool = False,
) -> None:
    """``waiting``: nothing was asked of EPIAS, so the attempt is returned."""
    clock = Clock(now)
    with transaction.atomic():
        job = _locked_job(lease.job.pk)
        now = clock.now()
        if job is None or not _owns(job, lease, now):
            return
        job.last_error = error
        if waiting:
            job.attempts -= 1
            _schedule_retry(job, now, BACK_OFF)
        elif not retryable:
            job.status = EpiasJobStatus.FAILED
        elif job.retry_until is not None:
            retry_at = next_retry_at(
                now,
                job.attempts,
                retry_after,
                minimum_seconds=int(NIGHT_RETRY_DELAY.total_seconds()),
            )
            _schedule_retry(job, now, retry_at - now)
        else:
            job.status = (
                EpiasJobStatus.FAILED
                if PROVIDER_JOB.exhausted(job.attempts)
                else EpiasJobStatus.PENDING
            )
            job.available_at = next_retry_at(now, job.attempts, retry_after)
        if not _current(job):
            job.status = EpiasJobStatus.SUPERSEDED
        release(job)
        job.save()


def _schedule_retry(job: EpiasCollectionJob, now: datetime, delay: timedelta) -> None:
    job.available_at = now + delay
    job.status = EpiasJobStatus.PENDING
    if (job.retry_until is not None and job.available_at >= job.retry_until) or (
        not _nightly_retry(job) and PROVIDER_JOB.exhausted(job.attempts)
    ):
        job.status = EpiasJobStatus.FAILED


def _publish(lease: EpiasLease, data: EpiasDay, now: datetime) -> bool:
    clock = Clock(now)
    raw = json.loads(data.raw_payload)
    end_day = lease.job.period_end_day or lease.job.target_date
    daily: list[tuple[date, dict[str, object], list[dict[str, object]], bool]] = []
    for offset in range((end_day - lease.job.target_date).days + 1):
        target_date = lease.job.target_date + timedelta(days=offset)
        rows = [row for row in data.readings if row.timestamp.date() == target_date]
        day_raw = raw
        if lease.job.source_kind == EpiasSourceKind.SETTLEMENT_INJECTION:
            day_raw = {
                "items": [
                    item
                    for item in raw["items"]
                    if datetime.fromisoformat(item["date"]).astimezone(TIMEZONE).date()
                    == target_date
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
        daily.append((target_date, day_raw, readings, complete))
    complete = all(item[3] for item in daily)
    with transaction.atomic():
        job = _locked_job(lease.job.pk)
        now = clock.now()
        if job is None or not _owns(job, lease, now):
            return False
        if not _current(job):
            job.status = EpiasJobStatus.SUPERSEDED
        else:
            for target_date, day_raw, readings, day_complete in daily:
                digest = hashlib.sha256(
                    json.dumps(day_raw, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                EpiasSnapshot.objects.get_or_create(
                    job=job,
                    attempt=job.attempts,
                    target_date=target_date,
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
                job.status = EpiasJobStatus.SUCCEEDED
                job.last_error = ""
            elif job.source_kind == EpiasSourceKind.SETTLEMENT_INJECTION:
                job.status = EpiasJobStatus.AWAITING_PUBLICATION
                job.last_error = (
                    EpiasJobError.UNPUBLISHED_DATA
                    if not data.readings
                    else EpiasJobError.PARTIAL_DATA
                )
            else:
                job.last_error = EpiasJobError.PARTIAL_DATA
                if job.retry_until is not None:
                    _schedule_retry(job, now, NIGHT_RETRY_DELAY)
                else:
                    job.status = (
                        EpiasJobStatus.FAILED
                        if PROVIDER_JOB.exhausted(job.attempts)
                        else EpiasJobStatus.PENDING
                    )
                    job.available_at = now + timedelta(minutes=30 * 2 ** (job.attempts - 1))
        release(job)
        job.save()
        if job.status != EpiasJobStatus.SUPERSEDED:
            from pyracast.plants.preparation import request_preparation

            request_preparation([job.connection.plant_id])
        return job.status == EpiasJobStatus.SUCCEEDED


def run_epias_job(
    job_id: int, *, fetch: Fetcher | None = None, now: datetime | None = None
) -> bool:
    clock = Clock(now or timezone.now())
    deadline = clock.started + LAST_REQUEST_START.total_seconds()
    lease = claim_epias_job(job_id, clock.entered)
    if lease is None:
        return False
    try:
        request_at = clock.now()
        if lease.job.retry_until is not None and request_at >= lease.job.retry_until:
            _finish_failure(lease, request_at, None, error=EpiasJobError.RETRY_WINDOW_EXPIRED)
            return False
        plant = EpiasPlant(**lease.job.provider_plant)
        if fetch is not None:
            data = fetch(plant, lease.job.target_date)
        elif lease.job.source_kind == EpiasSourceKind.SETTLEMENT_INJECTION:
            data = get_epias_client().fetch_settlement(
                plant,
                lease.job.target_date,
                lease.job.period_end_day or lease.job.target_date,
                deadline=deadline,
            )
        else:
            data = get_epias_client().fetch_day(plant, lease.job.target_date, deadline=deadline)
        if (
            data.plant.id != plant.id
            or data.plant.eic != plant.eic
            or data.day != lease.job.target_date
            or data.source_kind != lease.job.source_kind
            or data.end_day != lease.job.period_end_day
        ):
            raise EpiasError("epias_response_identity_mismatch")
        return _publish(lease, data, clock.now())
    except EpiasCredentialsError as exc:
        logger.warning("metering_credentials_unusable job=%s code=%s", job_id, exc.args[0])
        _finish_failure(
            lease,
            clock.now(),
            None,
            error=EpiasJobError(exc.args[0]),
            waiting=True,
        )
        return False
    except Exception as exc:
        log_failure(logger, "metering_job_failed", exc, job=job_id, attempt=lease.job.attempts)
        retry_after = exc.retry_after_seconds if isinstance(exc, EpiasError) else None
        _finish_failure(
            lease,
            clock.now(),
            retry_after,
            retryable=exc.retryable if isinstance(exc, EpiasError) else True,
        )
        return False


def pending_epias_ids(now: datetime, *, limit: int = 100) -> list[int]:
    eligible = Q(status__in=WAITING) | claimable(EpiasJobStatus.RUNNING, now)
    EpiasCollectionJob.objects.filter(eligible, retry_until__lte=now).update(
        status=EpiasJobStatus.FAILED,
        last_error=Case(
            When(last_error__in=CREDENTIAL_ERRORS, then=F("last_error")),
            default=Value(EpiasJobError.RETRY_WINDOW_EXPIRED),
        ),
        **RELEASED,
    )
    return list(
        EpiasCollectionJob.objects.filter(
            eligible,
            available_at__lte=now,
        )
        .order_by("available_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
