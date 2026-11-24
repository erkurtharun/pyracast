from zoneinfo import ZoneInfo

from pyracast.forecasts.models import CollectionEndpoint, CollectionSchedule
from pyracast.forecasts.timing import forecast_timing, latest_forecast_slot
from pyracast.local_time import local_slot
from pyracast.notifications.models import NotificationDelivery


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
    """The slot's plant-local clock when the schedule sends more than once on its day."""
    schedule = delivery.schedule
    zone = ZoneInfo(schedule.region.timezone)
    local = delivery.scheduled_for.astimezone(zone)
    clocks = [
        clock
        for clock in schedule.times[: delivery.binding.provider_account.daily_quota]
        if local_slot(local.date(), clock, zone) is not None
    ]
    return local.strftime("%H:%M") if len(clocks) > 1 else None
