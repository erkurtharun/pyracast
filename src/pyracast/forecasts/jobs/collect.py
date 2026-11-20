import logging
import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from django.db import DatabaseError, transaction

from pyracast.background.clock import Clock
from pyracast.background.failures import log_failure
from pyracast.background.lease import RELEASED, owns
from pyracast.connections.contracts import (
    Calculator,
    HistoryAdapter,
    ProviderAdapter,
    ProviderData,
    ProviderError,
    WindowHistoryAdapter,
)
from pyracast.forecasts.acquisition import missing_actuals_window
from pyracast.forecasts.jobs.claim import Failure, Lease, RetryKind, claim, retry
from pyracast.forecasts.jobs.publish import PublicationHooks, publish
from pyracast.forecasts.models import (
    CollectionEndpoint,
    CollectionJob,
    CollectionJobError,
    CollectionJobStatus,
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
    BindingDayExhaustedError,
    ForecastBudgetReservedError,
    HistoryWindowClosedError,
    QuotaExceededError,
    RequestDeferredError,
    ScheduleDisabledError,
    record_provider_failure,
    reserve_request,
)
from pyracast.forecasts.sources import snapshot_data
from pyracast.irradiance import local_day_bounds
from pyracast.provider_retry import rate_limit_retry_after
from pyracast.weather import OpenMeteoProvider
from pyracast.weather_data import WeatherError, serialize_weather

logger = logging.getLogger(__name__)

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


class CollectionFailureError(Exception):
    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.code)
        self.failure = failure


@dataclass
class _Progress:
    clock: Clock
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
    if isinstance(error, ScheduleDisabledError):
        return Failure(CollectionJobError.SCHEDULE_DISABLED, RetryKind.SKIPPED)
    if isinstance(error, BindingDayExhaustedError):
        return Failure(CollectionJobError.QUOTA_EXHAUSTED, RetryKind.SKIPPED)
    return Failure(CollectionJobError.QUOTA_EXHAUSTED, RetryKind.PROVIDER_DAY)


def persist_snapshot(lease: Lease, data: ProviderData, now: datetime) -> ForecastSnapshot | None:
    clock = Clock(now)
    with transaction.atomic():
        job = CollectionJob.objects.select_for_update().get(pk=lease.job_id)
        now = clock.now()
        if not owns(job, lease.token, CollectionJobStatus.RUNNING, now):
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


def fetch_history(
    lease: Lease,
    adapter: ProviderAdapter,
    *,
    window: tuple[datetime, datetime],
    now: datetime,
) -> ProviderData:
    if isinstance(adapter, WindowHistoryAdapter):
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
    clock = Clock(now)
    with transaction.atomic():
        job = CollectionJob.objects.select_for_update().get(pk=lease.job_id)
        now = clock.now()
        if not owns(job, lease.token, CollectionJobStatus.RUNNING, now):
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
    progress: _Progress,
    history_window: tuple[datetime, datetime] | None,
    weather_provider: OpenMeteoProvider | None,
) -> ForecastSnapshot | None:
    clock = progress.clock.now
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
            if history_window is not None
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


def _collect_leased[Prepared](
    lease: Lease,
    adapter: ProviderAdapter,
    calculator: Calculator,
    hooks: PublicationHooks[Prepared],
    progress: _Progress,
    weather_provider: OpenMeteoProvider | None,
) -> bool:
    clock = progress.clock.now
    actuals = lease.endpoint == CollectionEndpoint.ESTIMATED_ACTUALS
    snapshot = ForecastSnapshot.objects.filter(job_id=lease.job_id).first()
    if snapshot is None:
        history_window = (
            missing_actuals_window(lease.binding, progress.clock.entered) if actuals else None
        )
        if actuals and history_window is None:
            CollectionJob.objects.filter(
                pk=lease.job_id, lease_token=lease.token, status=CollectionJobStatus.RUNNING
            ).update(
                status=CollectionJobStatus.SUCCEEDED,
                last_error=CollectionJobError.HISTORY_ALREADY_ARCHIVED,
                **RELEASED,
            )
            return True
        snapshot = _acquire(
            lease,
            adapter,
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
        return publish(lease, snapshot, {}, clock(), hooks=hooks)
    try:
        data = snapshot_data(snapshot)
        payload = calculator(lease.binding, lease.target_date, data)
        prepared_issuances = hooks.prepare(snapshot)
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
        hooks=hooks,
        daily_payloads=daily_payloads,
        prepared_issuances=prepared_issuances,
    )


def collect[Prepared](
    job_id: int,
    adapter: ProviderAdapter,
    calculator: Calculator,
    now: datetime,
    *,
    hooks: PublicationHooks[Prepared],
    weather_provider: OpenMeteoProvider | None = None,
) -> bool:
    progress = _Progress(Clock(now))
    lease = claim(job_id, now)
    if lease is None:
        return False
    try:
        return _collect_leased(lease, adapter, calculator, hooks, progress, weather_provider)
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
    retry(lease, progress.clock.now(), failure)
    return False
