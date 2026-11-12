from datetime import datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from django.db.models import Q

from pyracast.forecasts.models import CollectionJob, CollectionSchedule, ForecastSnapshot
from pyracast.forecasts.projections import saved_day_payload
from pyracast.notifications.mail import build_message, message_payload
from pyracast.notifications.models import NotificationDelivery


def prepare_delivery(delivery: NotificationDelivery, now: datetime) -> bool:
    if delivery.prepared_message is not None:
        return True
    binding = delivery.binding
    assert binding is not None  # noqa: S101 -- Authority was checked under the region lock.
    deadline = delivery.preparation_deadline or delivery.scheduled_for
    slot_deliveries = NotificationDelivery.objects.filter(
        preference__region_id=delivery.preference.region_id,
        binding=binding,
        scheduled_for=delivery.scheduled_for,
    )
    prepared = slot_deliveries.filter(prepared_message__isnull=False).first()
    if prepared is not None:
        delivery.snapshot = prepared.snapshot
        delivery.prepared_message = prepared.prepared_message
        slot_deliveries.filter(prepared_message__isnull=True, attempts=0).update(
            snapshot=delivery.snapshot, prepared_message=delivery.prepared_message
        )
        return True
    job = CollectionJob.objects.filter(
        binding=binding,
        endpoint="forecasts",
        reference_import__isnull=True,
        scheduled_slot=delivery.scheduled_for,
    ).first()
    schedule = CollectionSchedule.objects.filter(
        region_id=binding.region_id, enabled=True, effective_from__lte=delivery.scheduled_for
    ).first()
    clock = delivery.scheduled_for.astimezone(ZoneInfo(binding.timezone)).strftime("%H:%M")
    due = schedule is not None and clock in schedule.times
    if now < deadline and (
        (job is not None and job.status in {"pending", "running", "retry"}) or (job is None and due)
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
        ForecastSnapshot.objects.filter(
            binding=binding,
            job__status="succeeded",
            source_kind="forecast",
            collected_at__lte=cutoff,
            forecastresult__published_at__lte=cutoff,
        )
        .filter(
            Q(
                collected_at__lte=delivery.scheduled_for,
                forecastresult__published_at__lte=delivery.scheduled_for,
            )
            | Q(job__scheduled_slot=delivery.scheduled_for, job__reference_import__isnull=True)
        )
        .select_related("forecastresult")
        .order_by("-collected_at", "-pk")
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
    results: list[dict[str, object] | None] = (
        [saved_day_payload(delivery.snapshot, day) for day in (today, tomorrow)]
        if delivery.snapshot is not None
        else [None, None]
    )
    payload: dict[str, object] = dict(
        message_payload(build_message(delivery, results, prepared_at=now))
    )
    payload["prepared_at"] = now.isoformat()
    payload["tomorrow_complete"] = results[1] is not None and results[1]["complete"] is True
    payload["kind"] = (
        "current"
        if snapshot and snapshot.job_id == getattr(job, "pk", None)
        else "saved_complete"
        if snapshot
        else "saved_partial"
        if partial
        else "unavailable"
    )
    slot_deliveries.filter(prepared_message__isnull=True, attempts=0).update(
        snapshot=delivery.snapshot, prepared_message=payload
    )
    delivery.prepared_message = payload
    return True
