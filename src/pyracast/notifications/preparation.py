from datetime import datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from django.db.models import Q

from pyracast.forecasts.models import (
    CollectionEndpoint,
    CollectionJob,
    CollectionJobStatus,
    CollectionSchedule,
    ForecastSnapshot,
)
from pyracast.forecasts.projections import saved_day_payload
from pyracast.forecasts.timing import effective_forecast_schedule, latest_forecast_slot
from pyracast.notifications.mail import build_message, message_payload
from pyracast.notifications.models import NotificationDelivery, PreparedMessageKind
from pyracast.notifications.presentation import report_summary
from pyracast.notifications.report_data import saved_report_context
from pyracast.notifications.sources import eligible_saved_forecasts, saved_report_results


def prepare_delivery(delivery: NotificationDelivery, now: datetime) -> bool:
    if delivery.prepared_message is not None:
        return True
    binding = delivery.binding
    deadline = delivery.preparation_deadline
    jobs = list(
        CollectionJob.objects.filter(
            binding=binding,
            endpoint__in=(CollectionEndpoint.FORECASTS, CollectionEndpoint.ESTIMATED_ACTUALS),
            reference_import__isnull=True,
            scheduled_slot=delivery.scheduled_for,
        )
    )
    job = next(
        (candidate for candidate in jobs if candidate.endpoint == CollectionEndpoint.FORECASTS),
        None,
    )
    schedule = CollectionSchedule.objects.filter(region_id=binding.region_id).first()
    first_slot = (
        CollectionJob.objects.filter(
            binding=binding, endpoint=CollectionEndpoint.FORECASTS, reference_import__isnull=True
        )
        .order_by("scheduled_slot")
        .values_list("scheduled_slot", flat=True)
        .first()
    )
    timing = effective_forecast_schedule(
        first_slot=first_slot, schedule=schedule, daily_quota=binding.provider_account.daily_quota
    )
    due = (
        timing is not None
        and latest_forecast_slot(
            delivery.scheduled_for, binding.timezone, timing.times, timing.effective_from
        )
        == delivery.scheduled_for
    )
    if now < deadline and (
        any(
            candidate.status
            in {CollectionJobStatus.PENDING, CollectionJobStatus.RUNNING, CollectionJobStatus.RETRY}
            and (candidate.status != CollectionJobStatus.RETRY or candidate.available_at < deadline)
            for candidate in jobs
        )
        or (job is None and due)
    ):
        delivery.available_at = min(now + timedelta(seconds=30), deadline)
        delivery.save(update_fields=("available_at",))
        return False
    cutoff = min(now, deadline)
    today = delivery.scheduled_for.astimezone(ZoneInfo(binding.timezone)).date()
    tomorrow = today + timedelta(days=1)
    partial: ForecastSnapshot | None = None
    snapshot: ForecastSnapshot | None = None
    for candidate in (
        eligible_saved_forecasts(delivery, cutoff)
        .filter(
            Q(forecastresult__daily_payloads__has_key=tomorrow.isoformat())
            | Q(forecastresult__payload__local_date=tomorrow.isoformat())
        )
        .defer("raw_payload", "normalized_intervals", "weather_payload")
    ):
        result = saved_day_payload(candidate, tomorrow)
        if result is None:
            continue
        if result["complete"] is True:
            snapshot = candidate
            break
        if partial is None and cast(float, result["valid_coverage_seconds"]) > 0:
            partial = candidate
    delivery.snapshot = snapshot or partial
    results = saved_report_results(delivery, cutoff)
    kind = (
        PreparedMessageKind.CURRENT
        if snapshot and snapshot.job_id == getattr(job, "pk", None)
        else PreparedMessageKind.SAVED_COMPLETE
        if snapshot
        else PreparedMessageKind.SAVED_PARTIAL
        if partial
        else PreparedMessageKind.UNAVAILABLE
    )
    _freeze(delivery, results, now, kind)
    return True


def _freeze(
    delivery: NotificationDelivery,
    results: list[dict[str, object] | None],
    prepared_at: datetime,
    kind: str,
) -> None:
    context = saved_report_context(delivery, available_at=prepared_at)
    electrical = context["plant_id"] is not None
    tomorrow = cast(list[dict[str, object]], context["days"])[1]
    result = cast(dict[str, object] | None, tomorrow["normal"]) if electrical else results[1]
    unit = "mwh" if electrical else "kwh_m2"
    payload: dict[str, object] = dict(
        message_payload(
            build_message(delivery, results, prepared_at=prepared_at, report_context=context)
        )
    )
    payload.update(
        prepared_at=prepared_at.isoformat(),
        kind=kind,
        results=results,
        report_context=context,
        tomorrow_complete=report_summary(result).get(f"energy_{unit}") is not None,
    )
    delivery.prepared_message = payload
    NotificationDelivery.objects.filter(
        pk=delivery.pk, prepared_message__isnull=True, attempts=0
    ).update(snapshot=delivery.snapshot, prepared_message=payload)
