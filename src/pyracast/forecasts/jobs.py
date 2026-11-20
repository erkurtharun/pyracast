import logging
import math
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.db.models import Q

from pyracast.background.deadlines import ACTORS
from pyracast.background.failures import log_failure
from pyracast.collection_window import INTRADAY_HISTORY_WINDOW, NIGHT_RETRY_DELAY
from pyracast.connections.contracts import (
    Calculator,
    HistoryAdapter,
    ProviderAdapter,
    ProviderData,
    ProviderError,
    WindowHistoryAdapter,
)
from pyracast.connections.currency import binding_is_current
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.acquisition import missing_actuals_window
from pyracast.forecasts.models import (
    CollectionEndpoint,
    CollectionJob,
    CollectionJobError,
    CollectionJobStatus,
    CollectionSchedule,
    ForecastResult,
    ForecastSnapshot,
    RequestAttempt,
    RequestOutcome,
    RequestPurpose,
    SnapshotSourceKind,
    SnapshotWeatherError,
)
from pyracast.forecasts.projections import prepare_daily_payloads
from pyracast.forecasts.quota import (
    AccountUnavailableError,
    ForecastBudgetReservedError,
    HistoryWindowClosedError,
    QuotaExceededError,
    RequestDeferredError,
    record_provider_failure,
    reserve_request,
)
from pyracast.forecasts.sources import snapshot_data
from pyracast.forecasts.timing import forecast_timing, latest_forecast_slot
from pyracast.irradiance import local_day_bounds
from pyracast.plants.models import Plant
from pyracast.provider_retry import next_provider_day, next_retry_at, rate_limit_retry_after
from pyracast.regions.models import Region, active_region
from pyracast.weather import OpenMeteoProvider
from pyracast.weather_data import WeatherError, serialize_weather

if TYPE_CHECKING:
    from pyracast.plants.forecast_archive import PreparedForecastIssuance

logger = logging.getLogger(__name__)

LEASE_DURATION = ACTORS["collect_forecast"].lease
MAX_ATTEMPTS = 3
LOCK_SQLSTATES = frozenset({"55P03", "40P01"})
INVALID_ANSWER_CODES = frozenset(
    {
        "invalid_provider_endpoint",
        "invalid_provider_interval",
        "invalid_provider_schema",
        "provider_response_too_large",
        "unsafe_provider_response",
    }
)
PROVIDER_STATUS_OUTCOMES = {
    401: CollectionJobError.PROVIDER_AUTHENTICATION_FAILED,
    402: CollectionJobError.PROVIDER_ALLOWANCE_EXHAUSTED,
    403: CollectionJobError.PROVIDER_ACCESS_DENIED,
    404: CollectionJobError.PROVIDER_SITE_NOT_FOUND,
    429: CollectionJobError.PROVIDER_THROTTLED,
}


@dataclass(frozen=True)
class Lease:
    job_id: int
    binding: SiteBinding
    target_date: date
    token: uuid.UUID
    endpoint: str = "forecasts"


class RetryKind(StrEnum):
    # Counts the attempt and backs off; the job fails after MAX_ATTEMPTS or its window.
    TRANSIENT = "transient"
    # Transient provider failure: as TRANSIENT with the forecast minimum backoff.
    PROVIDER = "provider"
    # Returns the attempt and backs off: nothing was asked of the provider.
    WAITING = "waiting"
    # Returns the attempt and waits for the provider day; past its window the job is skipped.
    PROVIDER_DAY = "provider_day"
    # Returns the attempt and fails: an operator must act first or the job's window has closed.
    BLOCKED = "blocked"
    PERMANENT = "permanent"


@dataclass(frozen=True)
class Failure:
    code: CollectionJobError
    kind: RetryKind = RetryKind.TRANSIENT
    retry_after_seconds: int | None = None


class CollectionFailureError(Exception):
    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.code)
        self.failure = failure


@dataclass
class _Progress:
    started: float = field(default_factory=lambda: time.monotonic())
    reserved: bool = False


def provider_failure(error: ProviderError) -> Failure:
    status = error.status_code
    kind = RetryKind.PROVIDER if error.retryable else RetryKind.PERMANENT
    if status == 402:
        return Failure(CollectionJobError.PROVIDER_ALLOWANCE_EXHAUSTED, RetryKind.PROVIDER_DAY)
    if status == 429:
        return Failure(
            CollectionJobError.PROVIDER_THROTTLED,
            retry_after_seconds=rate_limit_retry_after(error.retry_after_seconds),
        )
    if status in PROVIDER_STATUS_OUTCOMES:
        return Failure(PROVIDER_STATUS_OUTCOMES[status], kind, error.retry_after_seconds)
    if status is not None and 500 <= status < 600:
        return Failure(CollectionJobError.PROVIDER_SERVER_ERROR, kind, error.retry_after_seconds)
    if status is None and error.code == "provider_request_failed":
        return Failure(CollectionJobError.PROVIDER_UNREACHABLE, kind)
    if status is None and error.code in INVALID_ANSWER_CODES:
        return Failure(CollectionJobError.PROVIDER_INVALID_RESPONSE, kind)
    return Failure(CollectionJobError.PROVIDER_FAILURE, kind, error.retry_after_seconds)


def _refusal(error: QuotaExceededError, now: datetime) -> Failure:
    if isinstance(error, RequestDeferredError):
        if error.available_at is None:
            return Failure(CollectionJobError.PROVIDER_AUTHENTICATION_FAILED, RetryKind.BLOCKED)
        return Failure(
            CollectionJobError.PROVIDER_THROTTLED,
            RetryKind.WAITING,
            math.ceil((error.available_at - now).total_seconds()),
        )
    if isinstance(error, HistoryWindowClosedError):
        return Failure(CollectionJobError.HISTORY_WINDOW_CLOSED, RetryKind.BLOCKED)
    if isinstance(error, ForecastBudgetReservedError):
        return Failure(CollectionJobError.HISTORY_BUDGET_RESERVED, RetryKind.PROVIDER_DAY)
    if isinstance(error, AccountUnavailableError):
        return Failure(CollectionJobError.ACCOUNT_UNAVAILABLE, RetryKind.PROVIDER_DAY)
    return Failure(CollectionJobError.QUOTA_EXHAUSTED, RetryKind.PROVIDER_DAY)


def _is_intraday(job: CollectionJob) -> bool:
    return job.retry_until is not None and (
        job.retry_until <= job.scheduled_for + INTRADAY_HISTORY_WINDOW
    )


def _end_unclaimed(
    job: CollectionJob, status: CollectionJobStatus, code: CollectionJobError
) -> None:
    job.status = status
    job.last_error = code
    job.lease_token = None
    job.lease_expires_at = None
    job.save(update_fields=("status", "last_error", "lease_token", "lease_expires_at"))


def _stale_reason(
    job: CollectionJob, binding: SiteBinding, now: datetime
) -> CollectionJobError | None:
    schedule = CollectionSchedule.objects.filter(region_id=binding.region_id).first()
    timing = forecast_timing(binding, schedule)
    latest_slot = (
        latest_forecast_slot(now, binding.timezone, timing.times, timing.effective_from)
        if timing is not None
        else None
    )
    if (
        job.endpoint == CollectionEndpoint.FORECASTS
        and latest_slot is not None
        and latest_slot > job.scheduled_for
    ):
        return CollectionJobError.FORECAST_SUPERSEDED
    if not binding_is_current(binding):
        return CollectionJobError.BINDING_NOT_CURRENT
    if schedule is None:
        return None
    if not schedule.enabled:
        return CollectionJobError.SCHEDULE_DISABLED
    slot_bound = job.endpoint == CollectionEndpoint.FORECASTS or (
        job.endpoint == CollectionEndpoint.ESTIMATED_ACTUALS
        and job.target_date == job.scheduled_for.astimezone(ZoneInfo(binding.timezone)).date()
    )
    clock = job.scheduled_for.astimezone(ZoneInfo(binding.region.timezone)).strftime("%H:%M")
    # Approval creates one immediate acquisition outside existing clocks.
    approval_acquisition = (
        job.endpoint == CollectionEndpoint.FORECASTS
        and job.scheduled_for >= schedule.effective_from
        and timing is not None
        and job.pk == timing.first_job_id
    )
    if (
        slot_bound
        and clock not in (timing.times if timing is not None else ())
        and not approval_acquisition
    ):
        return CollectionJobError.SLOT_NOT_CONFIGURED
    return None


def claim(job_id: int, now: datetime) -> Lease | None:
    started = time.monotonic()
    if now.tzinfo is None:
        raise ValidationError("An aware timestamp is required.")
    with transaction.atomic():
        region_id = (
            CollectionJob.objects.filter(pk=job_id)
            .values_list("binding__region_id", flat=True)
            .first()
        )
        if region_id is not None:
            Region.objects.select_for_update().get(pk=region_id)
        job = CollectionJob.objects.select_for_update().filter(pk=job_id).first()
        if job is None:
            return None
        now += timedelta(seconds=time.monotonic() - started)
        cleared_cooldown = (
            job.status == CollectionJobStatus.RETRY
            and job.last_error == CollectionJobError.PROVIDER_THROTTLED
            and CollectionJob.objects.filter(
                pk=job.pk, binding__provider_account__request_blocked_until__isnull=True
            ).exists()
        )
        if job.status in {
            CollectionJobStatus.SUCCEEDED,
            CollectionJobStatus.STALE,
            CollectionJobStatus.FAILED,
        } or (job.available_at > now and not cleared_cooldown):
            return None
        if job.retry_until is not None and now >= job.retry_until:
            _end_unclaimed(
                job, CollectionJobStatus.FAILED, CollectionJobError.HISTORY_WINDOW_CLOSED
            )
            return None
        if (
            job.status == CollectionJobStatus.RUNNING
            and job.lease_expires_at is not None
            and (job.lease_expires_at > now)
        ):
            return None
        if (
            job.endpoint == CollectionEndpoint.ESTIMATED_ACTUALS
            and CollectionJob.objects.filter(
                binding_id=job.binding_id,
                endpoint=CollectionEndpoint.ESTIMATED_ACTUALS,
                status=CollectionJobStatus.RUNNING,
                lease_expires_at__gt=now,
            )
            .exclude(pk=job.pk)
            .exists()
        ):
            return None
        binding = SiteBinding.objects.select_related("provider_account", "region").get(
            pk=job.binding_id
        )
        reason = _stale_reason(job, binding, now)
        if reason == CollectionJobError.FORECAST_SUPERSEDED:
            _end_unclaimed(job, CollectionJobStatus.STALE, reason)
            return None
        if job.retry_until is None and job.attempts >= MAX_ATTEMPTS:
            _end_unclaimed(job, CollectionJobStatus.FAILED, CollectionJobError.ATTEMPTS_EXHAUSTED)
            return None
        if reason is not None:
            _end_unclaimed(job, CollectionJobStatus.STALE, reason)
            return None
        token = uuid.uuid4()
        job.status = CollectionJobStatus.RUNNING
        job.lease_token = token
        job.lease_expires_at = now + LEASE_DURATION
        job.attempts += 1
        if cleared_cooldown:
            job.available_at = now
        if job.retry_until is not None:
            job.available_at = now + (LEASE_DURATION if _is_intraday(job) else NIGHT_RETRY_DELAY)
        job.save(
            update_fields=("status", "lease_token", "lease_expires_at", "attempts", "available_at")
        )
        return Lease(job.pk, binding, job.target_date, token, job.endpoint)


def _retry_at(job: CollectionJob, now: datetime, failure: Failure) -> datetime | None:
    """The next attempt, or None when the job ends."""
    waits_for_provider_day = failure.kind == RetryKind.PROVIDER_DAY
    if job.retry_until is not None:
        available = next_retry_at(
            now,
            job.attempts,
            failure.retry_after_seconds,
            minimum_seconds=120 * 2 ** min(job.attempts, 6)
            if _is_intraday(job) and not waits_for_provider_day
            else int(NIGHT_RETRY_DELAY.total_seconds()),
        )
        if waits_for_provider_day and failure.code != CollectionJobError.HISTORY_BUDGET_RESERVED:
            available = max(available, next_provider_day(now))
        return available if available < job.retry_until else None
    if waits_for_provider_day:
        return next_provider_day(now)
    if job.attempts >= MAX_ATTEMPTS:
        return None
    retry_after = failure.retry_after_seconds
    if failure.kind == RetryKind.PROVIDER and job.endpoint == CollectionEndpoint.FORECASTS:
        retry_after = max(retry_after or 0, 120 * 2 ** min(job.attempts, 6))
    return next_retry_at(now, job.attempts, retry_after)


def retry(lease: Lease, now: datetime, failure: Failure) -> None:
    with transaction.atomic():
        job = CollectionJob.objects.select_for_update().get(pk=lease.job_id)
        if job.lease_token != lease.token or job.status != CollectionJobStatus.RUNNING:
            return
        job.last_error = failure.code
        if failure.kind in {RetryKind.WAITING, RetryKind.PROVIDER_DAY, RetryKind.BLOCKED}:
            job.attempts -= 1
        available = (
            None
            if failure.kind in {RetryKind.BLOCKED, RetryKind.PERMANENT}
            else _retry_at(job, now, failure)
        )
        if available is not None:
            job.status = CollectionJobStatus.RETRY
            job.available_at = available
        elif failure.kind == RetryKind.PROVIDER_DAY:
            # A refusal that outlives the job's window is a planned skip, not a failure.
            job.status = CollectionJobStatus.STALE
        else:
            job.status = CollectionJobStatus.FAILED
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
                "binding": job.binding,
                "raw_payload": data.raw_payload,
                "normalized_intervals": [interval.to_row() for interval in data.intervals],
                "collected_at": data.collected_at or now,
                "issued_at": data.issued_at,
                "source_kind": data.source_kind,
                "weather_payload": serialize_weather(data.weather) if data.weather else None,
                "weather_error": data.weather_error,
                "coverage_start": min(
                    (interval.start for interval in data.intervals), default=None
                ),
                "coverage_end": max((interval.end for interval in data.intervals), default=None),
            },
        )
        return snapshot


def publish(
    lease: Lease,
    snapshot: ForecastSnapshot,
    payload: dict[str, object],
    now: datetime,
    *,
    daily_payloads: dict[str, dict[str, object]] | None = None,
    prepared_issuances: tuple[PreparedForecastIssuance, ...] | None = None,
) -> bool:
    from pyracast.plants.forecast_archive import (
        prepare_forecast_issuances,
        save_forecast_issuances,
    )

    started = time.monotonic()
    if prepared_issuances is None:
        prepared_issuances = prepare_forecast_issuances(snapshot)
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
        plant = Plant.objects.select_for_update().filter(region_id=lease.binding.region_id).first()
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
                save_forecast_issuances(prepared_issuances, snapshot, now, plant=plant)
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
        job.lease_token = None
        job.lease_expires_at = None
        job.save(update_fields=("status", "last_error", "lease_token", "lease_expires_at"))
        if (
            job.status == CollectionJobStatus.SUCCEEDED
            and job.endpoint == CollectionEndpoint.ESTIMATED_ACTUALS
        ):
            from pyracast.plants.preparation import request_preparation

            request_preparation(
                Plant.objects.filter(
                    region_id=binding.region_id, current_configuration__binding=binding
                ).values_list("pk", flat=True),
                now,
            )
        return job.status == CollectionJobStatus.SUCCEEDED


def fetch_history(
    lease: Lease,
    adapter: ProviderAdapter,
    *,
    window: tuple[datetime, datetime] | None = None,
    now: datetime | None = None,
) -> ProviderData:
    if window is not None and now is not None and isinstance(adapter, WindowHistoryAdapter):
        return adapter.fetch_history_window(lease.binding, *window, now)
    if not isinstance(adapter, HistoryAdapter):
        raise ProviderError("history_not_supported")
    data = adapter.fetch_history(lease.binding, lease.target_date, 7)
    zone = ZoneInfo(lease.binding.timezone)
    start = datetime.combine(
        lease.target_date - timedelta(days=6), datetime.min.time(), zone
    ).astimezone(UTC)
    end = datetime.combine(
        lease.target_date + timedelta(days=1), datetime.min.time(), zone
    ).astimezone(UTC)
    if now is not None:
        end = min(end, now)
    return ProviderData(
        data.raw_payload,
        tuple(
            interval for interval in data.intervals if interval.start < end and interval.end > start
        ),
        data.issued_at,
        data.collected_at,
        data.source_kind,
    )


def _attach_weather(lease: Lease, data: ProviderData, provider: OpenMeteoProvider) -> ProviderData:
    day_start, day_end = local_day_bounds(lease.target_date, lease.binding.timezone)
    start = min((interval.start for interval in data.intervals), default=day_start)
    end = max((interval.end for interval in data.intervals), default=day_end)
    try:
        weather = provider.fetch(lease.binding.latitude, lease.binding.longitude, start, end)
    except WeatherError as exc:
        return replace(data, weather=None, weather_error=SnapshotWeatherError(exc.code))
    except Exception as exc:
        logger.warning("Weather acquisition failed error_type=%s", type(exc).__name__)
        return replace(data, weather=None, weather_error=SnapshotWeatherError.ACQUISITION_FAILED)
    return replace(data, weather=weather, weather_error="")


def _persist_weather(lease: Lease, data: ProviderData, now: datetime) -> bool:
    started = time.monotonic()
    with transaction.atomic():
        job = CollectionJob.objects.select_for_update().get(pk=lease.job_id)
        now += timedelta(seconds=time.monotonic() - started)
        if (
            job.lease_token != lease.token
            or job.lease_expires_at is None
            or job.lease_expires_at <= now
        ):
            return False
        return bool(
            ForecastSnapshot.objects.filter(job=job, available_at__isnull=True).update(
                weather_payload=serialize_weather(data.weather) if data.weather else None,
                weather_error=data.weather_error,
            )
        )


def _acquire(
    lease: Lease,
    adapter: ProviderAdapter,
    now: datetime,
    progress: _Progress,
    history_window: tuple[datetime, datetime] | None,
    weather_provider: OpenMeteoProvider | None,
) -> ForecastSnapshot | None:
    def clock() -> datetime:
        return now + timedelta(seconds=time.monotonic() - progress.started)

    try:
        adapter.prepare(lease.binding)
    except ProviderError as exc:
        logger.warning("Provider configuration invalid job_id=%s code=%s", lease.job_id, exc.code)
        raise CollectionFailureError(
            Failure(CollectionJobError.PROVIDER_CONFIGURATION_INVALID, RetryKind.PERMANENT)
        ) from None
    job = CollectionJob.objects.get(pk=lease.job_id)
    try:
        attempt = reserve_request(
            lease.binding,
            clock(),
            purpose=RequestPurpose.ESTIMATED_ACTUALS
            if lease.endpoint == CollectionEndpoint.ESTIMATED_ACTUALS
            else RequestPurpose.FORECAST,
            job=job,
        )
    except QuotaExceededError as exc:
        raise CollectionFailureError(_refusal(exc, clock())) from None
    progress.reserved = True
    request_at = clock()
    if job.retry_until is not None and request_at >= job.retry_until:
        RequestAttempt.objects.filter(pk=attempt.pk).update(outcome=RequestOutcome.FAILED)
        raise CollectionFailureError(
            Failure(CollectionJobError.HISTORY_WINDOW_CLOSED, RetryKind.BLOCKED)
        )
    try:
        data = (
            fetch_history(lease, adapter, window=history_window, now=request_at)
            if lease.endpoint == CollectionEndpoint.ESTIMATED_ACTUALS
            else adapter.fetch(lease.binding, lease.target_date)
        )
    except ProviderError as exc:
        RequestAttempt.objects.filter(pk=attempt.pk).update(
            outcome=RequestOutcome.FAILED, http_status=exc.status_code
        )
        record_provider_failure(lease.binding.provider_account, exc, clock())
        logger.warning(
            "Provider request failed correlation_id=%s status_code=%s",
            attempt.correlation_id,
            exc.status_code,
        )
        raise CollectionFailureError(provider_failure(exc)) from None
    except Exception as exc:
        logger.warning(
            "Provider acquisition interrupted correlation_id=%s error_type=%s",
            attempt.correlation_id,
            type(exc).__name__,
        )
        raise CollectionFailureError(
            Failure(CollectionJobError.PROVIDER_UNEXPECTED_FAILURE)
        ) from None
    RequestAttempt.objects.filter(pk=attempt.pk).update(outcome=RequestOutcome.SUCCEEDED)
    logger.info("Provider request succeeded correlation_id=%s", attempt.correlation_id)
    if weather_provider is not None:
        data = replace(
            data, weather=None, weather_error=SnapshotWeatherError.ACQUISITION_INTERRUPTED
        )
    snapshot = persist_snapshot(lease, data, clock())
    if snapshot is None:
        return None
    if weather_provider is not None:
        data = _attach_weather(lease, data, weather_provider)
        if not _persist_weather(lease, data, clock()):
            return None
        snapshot.refresh_from_db(fields=("weather_payload", "weather_error"))
    return snapshot


def _collect_leased(
    lease: Lease,
    adapter: ProviderAdapter,
    calculator: Calculator,
    now: datetime,
    progress: _Progress,
    weather_provider: OpenMeteoProvider | None,
) -> bool:
    from pyracast.plants.forecast_archive import prepare_forecast_issuances

    def clock() -> datetime:
        return now + timedelta(seconds=time.monotonic() - progress.started)

    actuals = lease.endpoint == CollectionEndpoint.ESTIMATED_ACTUALS
    snapshot = ForecastSnapshot.objects.filter(job_id=lease.job_id).first()
    if snapshot is None:
        history_window = missing_actuals_window(lease.binding, now) if actuals else None
        if actuals and history_window is None:
            CollectionJob.objects.filter(
                pk=lease.job_id, lease_token=lease.token, status=CollectionJobStatus.RUNNING
            ).update(
                status=CollectionJobStatus.SUCCEEDED,
                last_error=CollectionJobError.HISTORY_ALREADY_ARCHIVED,
                lease_token=None,
                lease_expires_at=None,
            )
            return True
        snapshot = _acquire(
            lease,
            adapter,
            now,
            progress,
            history_window,
            None if actuals else weather_provider,
        )
        if snapshot is None:
            return False
    expected_kind = SnapshotSourceKind.ESTIMATED_ACTUALS if actuals else SnapshotSourceKind.FORECAST
    if snapshot.source_kind != expected_kind:
        raise CollectionFailureError(Failure(CollectionJobError.INVALID_SOURCE_KIND))
    if actuals:
        return publish(lease, snapshot, {}, clock())
    try:
        data = snapshot_data(snapshot)
        payload = calculator(lease.binding, lease.target_date, data)
        prepared_issuances = prepare_forecast_issuances(snapshot)
    except Exception as exc:
        logger.warning(
            "Forecast calculation failed job_id=%s error_type=%s", lease.job_id, type(exc).__name__
        )
        raise CollectionFailureError(Failure(CollectionJobError.CALCULATION_FAILURE)) from None
    daily_payloads = prepare_daily_payloads(
        lease.binding, lease.target_date, data, calculator, payload
    )
    return publish(
        lease,
        snapshot,
        payload,
        clock(),
        daily_payloads=daily_payloads,
        prepared_issuances=prepared_issuances,
    )


def collect(
    job_id: int,
    adapter: ProviderAdapter,
    calculator: Calculator,
    now: datetime,
    *,
    weather_provider: OpenMeteoProvider | None = None,
) -> bool:
    progress = _Progress()
    lease = claim(job_id, now)
    if lease is None:
        return False
    try:
        return _collect_leased(lease, adapter, calculator, now, progress, weather_provider)
    except CollectionFailureError as exc:
        failure = exc.failure
    except DatabaseError as exc:
        log_failure(logger, "collection_database_failure", exc, job_id=job_id)
        lock = getattr(exc.__cause__, "sqlstate", None) in LOCK_SQLSTATES
        failure = (
            Failure(
                CollectionJobError.LOCK_CONTENTION,
                RetryKind.TRANSIENT if progress.reserved else RetryKind.WAITING,
            )
            if lock
            else Failure(CollectionJobError.PUBLICATION_FAILURE)
        )
    except Exception as exc:
        log_failure(logger, "collection_failure", exc, job_id=job_id)
        failure = Failure(CollectionJobError.PUBLICATION_FAILURE)
    retry(lease, now + timedelta(seconds=time.monotonic() - progress.started), failure)
    return False


def pending_job_ids(now: datetime, limit: int = 100) -> list[int]:
    return list(
        CollectionJob.objects.filter(
            active_region("binding__region__"),
            Q(available_at__lte=now)
            | Q(
                status=CollectionJobStatus.RETRY,
                last_error=CollectionJobError.PROVIDER_THROTTLED,
                binding__provider_account__request_blocked_until__isnull=True,
            ),
        )
        .filter(
            Q(status__in=(CollectionJobStatus.PENDING, CollectionJobStatus.RETRY))
            | Q(status=CollectionJobStatus.RUNNING, lease_expires_at__lte=now)
        )
        .order_by("available_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
