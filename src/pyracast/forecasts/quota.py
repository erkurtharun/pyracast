import time
from datetime import UTC, date, datetime, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef

from pyracast.connections.contracts import ProviderError
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.budget import remaining_forecast_requests
from pyracast.forecasts.models import (
    CollectionJob,
    CollectionSchedule,
    RequestAttempt,
    RequestPurpose,
)
from pyracast.forecasts.timing import forecast_timing
from pyracast.provider_retry import next_provider_day, next_retry_at, rate_limit_retry_after


class QuotaExceededError(Exception):
    pass


class ForecastBudgetReservedError(QuotaExceededError):
    pass


class HistoryWindowClosedError(QuotaExceededError):
    pass


class AccountUnavailableError(QuotaExceededError):
    pass


class BindingDayExhaustedError(QuotaExceededError):
    pass


class ScheduleDisabledError(QuotaExceededError):
    pass


class RequestDeferredError(QuotaExceededError):
    def __init__(self, available_at: datetime | None) -> None:
        super().__init__("Provider requests are temporarily unavailable.")
        self.available_at = available_at


def _admit_forecast(
    binding: SiteBinding, account: ProviderAccount, job: CollectionJob, utc_day: date
) -> None:
    """A binding starts at most as many forecast jobs per UTC day as it has clocks, however often
    they are edited; a job counts on the UTC day of its first request, so its retries and the
    approval collection are free."""
    timing = forecast_timing(
        binding,
        CollectionSchedule.objects.filter(region_id=binding.region_id).first(),
        daily_quota=account.daily_quota,
    )
    if timing is None:
        raise ScheduleDisabledError
    if job.pk == timing.first_job_id or RequestAttempt.objects.filter(job=job).exists():
        return
    earlier = RequestAttempt.objects.filter(job_id=OuterRef("job_id"), utc_day__lt=utc_day)
    started = (
        RequestAttempt.objects.filter(
            binding=binding, purpose=RequestPurpose.FORECAST, utc_day=utc_day, job__isnull=False
        )
        .exclude(job_id=timing.first_job_id)
        .exclude(Exists(earlier))
        .values("job_id")
        .distinct()
        .count()
    )
    if started >= len(timing.times):
        raise BindingDayExhaustedError


def reserve_account_request(
    account: ProviderAccount,
    now: datetime,
    *,
    purpose: RequestPurpose,
    binding: SiteBinding | None = None,
    job: CollectionJob | None = None,
    keep_forecast_reserve: bool = False,
    credential_revision: int | None = None,
) -> RequestAttempt:
    """`credential_revision` names the key the caller sends; a replaced key spends nothing."""
    started = time.monotonic()
    if now.tzinfo is None:
        raise ValidationError("An aware timestamp is required.")
    if purpose not in RequestPurpose.values:
        raise ValidationError("Unknown request purpose.")
    if binding is not None and binding.provider_account_id != account.pk:
        raise ValidationError("The binding must belong to the provider account.")
    if job is not None and (binding is None or job.binding_id != binding.pk):
        raise ValidationError("The job must belong to the binding.")
    with transaction.atomic():
        account = ProviderAccount.objects.select_for_update().get(pk=account.pk)
        if credential_revision not in {None, account.credential_revision}:
            raise ValidationError("Configuration changed during validation.")
        now += timedelta(seconds=time.monotonic() - started)
        if job is not None and job.retry_until is not None and now >= job.retry_until:
            raise HistoryWindowClosedError
        if account.authentication_failed_revision == account.credential_revision:
            raise RequestDeferredError(None)
        if account.request_blocked_until is not None and account.request_blocked_until > now:
            raise RequestDeferredError(account.request_blocked_until)
        if not account.enabled or account.authorized_at is None:
            raise AccountUnavailableError
        utc_day = now.astimezone(UTC).date()
        used = RequestAttempt.objects.filter(provider_account=account, utc_day=utc_day).count()
        if used >= account.daily_quota:
            raise QuotaExceededError
        if job is not None and binding is not None and purpose == RequestPurpose.FORECAST:
            _admit_forecast(binding, account, job, utc_day)
        if (
            keep_forecast_reserve
            or (job is not None and purpose == RequestPurpose.ESTIMATED_ACTUALS)
        ) and used + 1 + remaining_forecast_requests(account, now) > account.daily_quota:
            raise ForecastBudgetReservedError
        return RequestAttempt.objects.create(
            provider_account=account,
            binding=binding,
            job=job,
            utc_day=utc_day,
            created_at=now,
            purpose=purpose,
        )


def reserve_request(
    binding: SiteBinding,
    now: datetime,
    *,
    purpose: RequestPurpose,
    job: CollectionJob | None = None,
    keep_forecast_reserve: bool = False,
    credential_revision: int | None = None,
) -> RequestAttempt:
    return reserve_account_request(
        binding.provider_account,
        now,
        purpose=purpose,
        binding=binding,
        job=job,
        keep_forecast_reserve=keep_forecast_reserve,
        credential_revision=credential_revision,
    )


def record_provider_failure(account: ProviderAccount, error: ProviderError, now: datetime) -> None:
    if error.status_code not in {401, 402, 429}:
        return
    with transaction.atomic():
        current = ProviderAccount.objects.select_for_update().get(pk=account.pk)
        if current.credential_revision != account.credential_revision:
            return
        if error.status_code == 401:
            current.authentication_failed_revision = account.credential_revision
        else:
            blocked_until = (
                next_provider_day(now)
                if error.status_code == 402
                else next_retry_at(now, 1, rate_limit_retry_after(error.retry_after_seconds))
            )
            current.request_blocked_until = max(current.request_blocked_until or now, blocked_until)
        current.save(update_fields=("authentication_failed_revision", "request_blocked_until"))
