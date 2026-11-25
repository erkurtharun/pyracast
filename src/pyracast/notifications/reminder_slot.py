from datetime import UTC
from zoneinfo import ZoneInfo

from pyracast.forecasts.models import CollectionEndpoint, CollectionSchedule
from pyracast.forecasts.timing import forecast_timing, latest_forecast_slot
from pyracast.irradiance import local_day_bounds
from pyracast.local_time import local_slot
from pyracast.notifications.models import NotificationDelivery, NotificationDeliveryStatus

_UNSENT = (
    NotificationDeliveryStatus.SKIPPED,
    NotificationDeliveryStatus.CANCELLED,
    NotificationDeliveryStatus.FAILED,
)


def collection_slot(delivery: NotificationDelivery) -> bool:
    """Whether the reminder's slot is also one of the binding's forecast collection slots."""
    binding = delivery.binding
    timing = forecast_timing(
        binding, CollectionSchedule.objects.filter(region_id=binding.region_id).first()
    )
    return (
        timing is not None
        and latest_forecast_slot(
            delivery.scheduled_for, binding.timezone, timing.times, timing.effective_from
        )
        == delivery.scheduled_for
    )


def missed_collection(delivery: NotificationDelivery) -> bool:
    """Whether the slot collects forecasts itself and tomorrow's forecast comes from another
    collection."""
    snapshot = delivery.snapshot
    if snapshot is None:
        return False
    job = snapshot.job
    own = (
        job.scheduled_for == delivery.scheduled_for
        and job.endpoint == CollectionEndpoint.FORECASTS
        and job.reference_import_id is None
    )
    return not own and collection_slot(delivery)


def edition(delivery: NotificationDelivery) -> str | None:
    """The slot's plant-local clock when its day has more than one send: the slots already
    created and not ended unsent, with those the schedule can still create."""
    schedule = delivery.schedule
    zone = ZoneInfo(schedule.region.timezone)
    local = delivery.scheduled_for.astimezone(zone)
    start, end = local_day_bounds(local.date(), schedule.region.timezone)
    created = NotificationDelivery.objects.filter(
        schedule=schedule, scheduled_for__gte=start, scheduled_for__lt=end
    ).exclude(status__in=_UNSENT)
    slots = {slot.astimezone(UTC) for slot in created.values_list("scheduled_for", flat=True)}
    slots.add(delivery.scheduled_for.astimezone(UTC))
    slots.update(
        slot
        for clock in schedule.times[: delivery.binding.provider_account.daily_quota]
        if (slot := local_slot(local.date(), clock, zone)) is not None
        and slot >= schedule.effective_from
    )
    return local.strftime("%H:%M") if len(slots) > 1 else None
