from datetime import datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from django.db.models import Q, QuerySet

from pyracast.forecasts.models import CollectionJob, CollectionSchedule, ForecastSnapshot
from pyracast.forecasts.projections import saved_day_payload
from pyracast.forecasts.timing import effective_forecast_schedule, latest_forecast_slot
from pyracast.notifications.mail import build_message, build_unavailable_message, message_payload
from pyracast.notifications.models import NotificationDelivery
from pyracast.notifications.presentation import report_summary
from pyracast.notifications.report_data import saved_report_context
from pyracast.notifications.sources import eligible_saved_forecasts, saved_report_results


def prepare_delivery(delivery: NotificationDelivery, now: datetime) -> bool:
    if delivery.prepared_message is not None:
        return True
    binding = delivery.binding
    assert binding is not None  # noqa: S101 -- Authority was checked under the region lock.
    deadline = delivery.preparation_deadline or delivery.scheduled_for
    slot_deliveries = NotificationDelivery.objects.filter(
        schedule_id=delivery.schedule_id,
        binding=binding,
        scheduled_for=delivery.scheduled_for,
    )
    prepared = slot_deliveries.filter(
        prepared_message__isnull=False, language=delivery.language
    ).first()
    if prepared is not None:
        delivery.snapshot = prepared.snapshot
        delivery.prepared_message = prepared.prepared_message
        slot_deliveries.filter(
            prepared_message__isnull=True, attempts=0, language=delivery.language
        ).update(snapshot=delivery.snapshot, prepared_message=delivery.prepared_message)
        return True
    frozen = slot_deliveries.filter(prepared_message__isnull=False).first()
    if frozen is not None:
        delivery.snapshot = frozen.snapshot
        existing = cast(dict[str, object], frozen.prepared_message)
        prepared_at = datetime.fromisoformat(
            cast(str, existing.get("prepared_at", delivery.scheduled_for.isoformat()))
        )
        metadata: dict[str, object] = {
            key: existing[key]
            for key in (
                "prepared_at",
                "tomorrow_complete",
                "kind",
                "format",
                "report_context",
                "results",
            )
            if key in existing
        }
        results = (
            cast(list[dict[str, object] | None], existing["results"])
            if "results" in existing
            else saved_report_results(delivery, prepared_at)
        )
        _freeze_slot(delivery, slot_deliveries, results, prepared_at, metadata)
        return True
    jobs = list(
        CollectionJob.objects.filter(
            binding=binding,
            endpoint__in=("forecasts", "estimated_actuals"),
            reference_import__isnull=True,
            scheduled_slot=delivery.scheduled_for,
        )
    )
    job = next((candidate for candidate in jobs if candidate.endpoint == "forecasts"), None)
    schedule = CollectionSchedule.objects.filter(region_id=binding.region_id).first()
    first_slot = (
        CollectionJob.objects.filter(
            binding=binding, endpoint="forecasts", reference_import__isnull=True
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
            candidate.status in {"pending", "running", "retry"}
            and (candidate.status != "retry" or candidate.available_at < deadline)
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
        "current"
        if snapshot and snapshot.job_id == getattr(job, "pk", None)
        else "saved_complete"
        if snapshot
        else "saved_partial"
        if partial
        else "unavailable"
    )
    _freeze_slot(
        delivery,
        slot_deliveries,
        results,
        now,
        {
            "prepared_at": now.isoformat(),
            "kind": kind,
        },
    )
    return True


def _freeze_slot(
    delivery: NotificationDelivery,
    slot_deliveries: QuerySet[NotificationDelivery],
    results: list[dict[str, object] | None],
    prepared_at: datetime,
    metadata: dict[str, object],
) -> None:
    metadata.setdefault("results", results)
    if metadata.get("format") != "plain" and "report_context" not in metadata:
        metadata["report_context"] = saved_report_context(delivery, available_at=prepared_at)
    if metadata.get("format") == "plain":
        metadata["tomorrow_complete"] = False
    else:
        context = cast(dict[str, object], metadata["report_context"])
        electrical = context["plant_id"] is not None
        tomorrow = cast(list[dict[str, object]], context["days"])[1]
        result = cast(dict[str, object] | None, tomorrow["normal"]) if electrical else results[1]
        unit = "mwh" if electrical else "kwh_m2"
        metadata["tomorrow_complete"] = report_summary(result).get(f"energy_{unit}") is not None
    payloads = {
        language: cast(dict[str, object], payload)
        for language, payload in slot_deliveries.filter(prepared_message__isnull=False)
        .order_by("language", "pk")
        .distinct("language")
        .values_list("language", "prepared_message")
    }
    unprepared = slot_deliveries.filter(prepared_message__isnull=True, attempts=0)
    for record in (delivery, *unprepared.select_related("schedule__region", "binding")):
        if record.language in payloads:
            continue
        record.snapshot = delivery.snapshot
        payload: dict[str, object] = dict(
            build_unavailable_message(record, prepared_at)[1]
            if metadata.get("format") == "plain"
            else message_payload(
                build_message(
                    record,
                    results,
                    prepared_at=prepared_at,
                    report_context=cast(dict[str, object] | None, metadata.get("report_context")),
                )
            )
        )
        payload.update(metadata)
        payloads[record.language] = payload
    for language, payload in payloads.items():
        unprepared.filter(language=language).update(
            snapshot=delivery.snapshot, prepared_message=payload
        )
    delivery.prepared_message = payloads[delivery.language]
