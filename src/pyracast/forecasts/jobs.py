import logging
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from pyracast.collection_window import NIGHT_RETRY_DELAY
from pyracast.connections.contracts import (
    Calculator,
    HistoryAdapter,
    ProviderAdapter,
    ProviderData,
    ProviderFailure,
)
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.connections.operations import (
    ForecastBudgetReserved,
    HistoryWindowClosed,
    QuotaExhausted,
    reserve_request,
)
from pyracast.forecasts.coverage import history_day_complete
from pyracast.forecasts.models import (
    CollectionJob,
    CollectionSchedule,
    ForecastResult,
    ForecastSnapshot,
    RequestAttempt,
)
from pyracast.forecasts.sources import snapshot_data
from pyracast.irradiance import local_day_bounds
from pyracast.regions.models import Region
from pyracast.weather import OpenMeteoProvider, WeatherFailure, serialize_weather

logger = logging.getLogger(__name__)

LEASE_DURATION = timedelta(minutes=5)


@dataclass(frozen=True)
class Lease:
    job_id: int
    binding: SiteBinding
    target_date: date
    token: uuid.UUID
    endpoint: str = "forecasts"


def binding_is_current(binding: SiteBinding) -> bool:
    return (
        Region.objects.filter(
            pk=binding.region_id,
            deleted_at__isnull=True,
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
        job = CollectionJob.objects.select_for_update().filter(pk=job_id).first()
        if job is None:
            return None
        now += timedelta(seconds=time.monotonic() - started)
        if job.status in {"succeeded", "stale", "failed"} or job.available_at > now:
            return None
        if job.retry_until is not None and now >= job.retry_until:
            job.status = "failed"
            job.last_error = "history_window_closed"
            job.lease_token = None
            job.lease_expires_at = None
            job.save(update_fields=("status", "last_error", "lease_token", "lease_expires_at"))
            return None
        if (
            job.status == "running"
            and job.lease_expires_at is not None
            and (job.lease_expires_at > now)
        ):
            return None
        binding = SiteBinding.objects.select_related("provider_account", "region").get(
            pk=job.binding_id
        )
        schedule = CollectionSchedule.objects.filter(region_id=binding.region_id).first()
        if not binding_is_current(binding) or (
            schedule is not None
            and (
                not schedule.enabled
                or (
                    job.endpoint == "forecasts"
                    and job.scheduled_slot.astimezone(ZoneInfo(binding.region.timezone)).strftime(
                        "%H:%M"
                    )
                    not in schedule.times
                    # Approval creates one immediate acquisition outside existing clocks.
                    and (
                        job.scheduled_slot < schedule.effective_from
                        or job.pk
                        != CollectionJob.objects.filter(binding=binding, endpoint="forecasts")
                        .order_by("scheduled_slot", "pk")
                        .values_list("pk", flat=True)
                        .first()
                    )
                )
            )
        ):
            job.status = "stale"
            job.save(update_fields=("status",))
            return None
        token = uuid.uuid4()
        job.status = "running"
        job.lease_token = token
        job.lease_expires_at = now + LEASE_DURATION
        job.attempts += 1
        if job.retry_until is not None:
            job.available_at = now + NIGHT_RETRY_DELAY
        job.save(
            update_fields=("status", "lease_token", "lease_expires_at", "attempts", "available_at")
        )
        return Lease(job.pk, binding, job.target_date, token, job.endpoint)


def retry(lease: Lease, now: datetime, code: str, *, quota: bool = False) -> None:
    with transaction.atomic():
        job = CollectionJob.objects.select_for_update().get(pk=lease.job_id)
        if job.lease_token != lease.token or job.status != "running":
            return
        job.last_error = code
        if job.retry_until is not None:
            job.available_at = now + NIGHT_RETRY_DELAY
            if quota and code != "history_budget_reserved":
                tomorrow = now.astimezone(UTC).date() + timedelta(days=1)
                reset = datetime.combine(tomorrow, datetime.min.time(), tzinfo=UTC)
                job.available_at = max(job.available_at, reset)
            job.status = "retry" if job.available_at < job.retry_until else "failed"
        elif quota:
            job.status = "retry"
            tomorrow = now.astimezone(UTC).date() + timedelta(days=1)
            job.available_at = datetime.combine(tomorrow, datetime.min.time(), tzinfo=UTC)
        else:
            job.status = "retry" if job.attempts < 3 else "failed"
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
                "binding": job.binding,
                "raw_payload": data.raw_payload,
                "normalized_intervals": data.intervals,
                "collected_at": data.collected_at or now,
                "issued_at": data.issued_at,
                "source_kind": data.source_kind,
                "weather_payload": serialize_weather(data.weather) if data.weather else None,
                "weather_error": data.weather_error,
                "coverage_start": min(
                    (datetime.fromisoformat(cast(str, row["start"])) for row in data.intervals),
                    default=None,
                ),
                "coverage_end": max(
                    (datetime.fromisoformat(cast(str, row["end"])) for row in data.intervals),
                    default=None,
                ),
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
        expected_kind = "estimated_actuals" if job.endpoint == "estimated_actuals" else "forecast"
        if snapshot.source_kind != expected_kind:
            raise ValidationError("Snapshot source must match the collection endpoint.")
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
            if job.endpoint == "forecasts":
                ForecastResult.objects.get_or_create(
                    snapshot=snapshot, defaults={"payload": payload, "published_at": now}
                )
            ForecastSnapshot.objects.filter(pk=snapshot.pk, available_at__isnull=True).update(
                available_at=now
            )
            job.status = "succeeded"
            job.last_error = ""
            if (
                job.endpoint == "estimated_actuals"
                and job.retry_until is not None
                and not history_day_complete(snapshot, job.target_date, binding.timezone)
            ):
                job.last_error = "history_incomplete"
                available = now + NIGHT_RETRY_DELAY
                schedule = CollectionSchedule.objects.filter(region_id=binding.region_id).first()
                if available < job.retry_until:
                    CollectionJob.objects.get_or_create(
                        binding=binding,
                        endpoint="estimated_actuals",
                        scheduled_slot=available,
                        defaults={
                            "target_date": job.target_date,
                            "available_at": available,
                            "retry_until": job.retry_until,
                            "status": "pending"
                            if schedule is None or schedule.enabled
                            else "stale",
                        },
                    )
        job.lease_token = None
        job.lease_expires_at = None
        job.save(update_fields=("status", "last_error", "lease_token", "lease_expires_at"))
        if job.status == "succeeded" and job.endpoint == "estimated_actuals":
            from pyracast.plants.models import Plant
            from pyracast.plants.preparation import prepare_calibration

            for plant_id in Plant.objects.filter(
                region_id=binding.region_id,
                deleted_at__isnull=True,
                current_configuration__binding=binding,
            ).values_list("pk", flat=True):
                prepare_calibration(plant_id, now)
        return job.status == "succeeded"


def fetch_history(lease: Lease, adapter: ProviderAdapter) -> ProviderData:
    if not isinstance(adapter, HistoryAdapter):
        raise ProviderFailure("history_not_supported")
    data = adapter.fetch_history(lease.binding, lease.target_date, 7)
    zone = ZoneInfo(lease.binding.timezone)
    start = datetime.combine(
        lease.target_date - timedelta(days=6), datetime.min.time(), zone
    ).astimezone(UTC)
    end = datetime.combine(
        lease.target_date + timedelta(days=1), datetime.min.time(), zone
    ).astimezone(UTC)
    return ProviderData(
        data.raw_payload,
        [
            row
            for row in data.intervals
            if datetime.fromisoformat(cast(str, row["start"])) < end
            and datetime.fromisoformat(cast(str, row["end"])) > start
        ],
        data.issued_at,
        data.collected_at,
        data.source_kind,
    )


def _attach_weather(lease: Lease, data: ProviderData, provider: OpenMeteoProvider) -> ProviderData:
    day_start, day_end = local_day_bounds(lease.target_date, lease.binding.timezone)
    start = min(
        (datetime.fromisoformat(cast(str, row["start"])) for row in data.intervals),
        default=day_start,
    )
    end = max(
        (datetime.fromisoformat(cast(str, row["end"])) for row in data.intervals), default=day_end
    )
    try:
        weather = provider.fetch(lease.binding.latitude, lease.binding.longitude, start, end)
    except WeatherFailure as exc:
        return replace(data, weather=None, weather_error=exc.code)
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


def collect(
    job_id: int,
    adapter: ProviderAdapter,
    calculator: Calculator,
    now: datetime,
    *,
    weather_provider: OpenMeteoProvider | None = None,
) -> bool:
    started = time.monotonic()
    lease = claim(job_id, now)
    if lease is None:
        return False
    weather_provider = weather_provider if lease.endpoint == "forecasts" else None
    snapshot = ForecastSnapshot.objects.filter(job_id=job_id).first()
    if snapshot is None:
        try:
            job = CollectionJob.objects.get(pk=job_id)
            attempt = reserve_request(
                lease.binding,
                now + timedelta(seconds=time.monotonic() - started),
                purpose="estimated_actuals"
                if lease.endpoint == "estimated_actuals"
                else "forecast",
                job=job,
            )
        except QuotaExhausted as exc:
            retry(
                lease,
                now + timedelta(seconds=time.monotonic() - started),
                "history_window_closed"
                if isinstance(exc, HistoryWindowClosed)
                else "history_budget_reserved"
                if isinstance(exc, ForecastBudgetReserved)
                else "quota_exhausted",
                quota=True,
            )
            return False
        request_at = now + timedelta(seconds=time.monotonic() - started)
        if job.retry_until is not None and request_at >= job.retry_until:
            RequestAttempt.objects.filter(pk=attempt.pk).update(outcome="failed")
            retry(lease, request_at, "history_window_closed", quota=True)
            return False
        try:
            data = (
                fetch_history(lease, adapter)
                if lease.endpoint == "estimated_actuals"
                else adapter.fetch(lease.binding, lease.target_date)
            )
        except ProviderFailure:
            RequestAttempt.objects.filter(pk=attempt.pk).update(outcome="failed")
            logger.warning("Provider request failed correlation_id=%s", attempt.correlation_id)
            retry(lease, now + timedelta(seconds=time.monotonic() - started), "provider_failure")
            return False
        RequestAttempt.objects.filter(pk=attempt.pk).update(outcome="succeeded")
        logger.info("Provider request succeeded correlation_id=%s", attempt.correlation_id)
        if weather_provider is not None:
            data = replace(data, weather=None, weather_error="weather_acquisition_interrupted")
        snapshot = persist_snapshot(
            lease, data, now + timedelta(seconds=time.monotonic() - started)
        )
        if snapshot is None:
            return False
        if weather_provider is not None:
            data = _attach_weather(lease, data, weather_provider)
            if not _persist_weather(
                lease, data, now + timedelta(seconds=time.monotonic() - started)
            ):
                return False
            snapshot.refresh_from_db(fields=("weather_payload", "weather_error"))
    expected_kind = "estimated_actuals" if lease.endpoint == "estimated_actuals" else "forecast"
    if snapshot.source_kind != expected_kind:
        retry(lease, now + timedelta(seconds=time.monotonic() - started), "invalid_source_kind")
        return False
    if lease.endpoint == "estimated_actuals":
        return publish(lease, snapshot, {}, now + timedelta(seconds=time.monotonic() - started))
    data = snapshot_data(snapshot)
    try:
        payload = calculator(lease.binding, lease.target_date, data)
    except ValueError, ArithmeticError:
        retry(lease, now + timedelta(seconds=time.monotonic() - started), "calculation_failure")
        return False
    return publish(lease, snapshot, payload, now + timedelta(seconds=time.monotonic() - started))


def pending_job_ids(now: datetime, limit: int = 100) -> list[int]:
    return list(
        CollectionJob.objects.filter(
            available_at__lte=now, binding__region__deleted_at__isnull=True
        )
        .filter(Q(status__in=("pending", "retry")) | Q(status="running", lease_expires_at__lte=now))
        .order_by("available_at", "pk")
        .values_list("pk", flat=True)[:limit]
    )
