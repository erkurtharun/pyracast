from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.utils import timezone

from pyracast.irradiance import local_day_bounds
from pyracast.plants.availability import (
    available_date_bounds,
    interval_is_covered,
    reference_intervals,
)
from pyracast.plants.models import Plant, PlantConfiguration, ProductionObservation
from pyracast.plants.operations import configuration_is_current
from pyracast.plants.serializers import ObservationOutput


def production_history(
    plant: Plant,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    resolution_minutes: int = 60,
) -> dict[str, Any]:
    configuration = (
        PlantConfiguration.objects.select_related("binding__provider_account").get(
            pk=plant.current_configuration_id
        )
        if plant.current_configuration_id is not None
        else None
    )
    zone = configuration.binding.timezone if configuration else plant.region.timezone
    now = timezone.now()
    first, last = available_date_bounds(configuration, now) if configuration else (None, None)
    if (start_date is None) != (end_date is None):
        raise ValidationError("Both start_date and end_date are required.")
    if start_date is None or end_date is None:
        end_date = last or now.astimezone(ZoneInfo(zone)).date() - timedelta(days=1)
        start_date = max(first or end_date - timedelta(days=6), end_date - timedelta(days=6))
    if resolution_minutes not in {30, 60} or not 1 <= (end_date - start_date).days + 1 <= 31:
        raise ValidationError("Use a range of 1 to 31 days and 30 or 60 minute resolution.")
    range_start, _ = local_day_bounds(start_date, zone)
    _, range_end = local_day_bounds(end_date, zone)
    observations = list(
        ProductionObservation.objects.filter(
            plant=plant, starts_at__lt=range_end, ends_at__gt=range_start
        ).order_by("starts_at", "id")
    )
    encoded = {row.pk: ObservationOutput(row).data for row in observations}
    current = configuration is not None and configuration_is_current(configuration)
    days, intervals = [], []
    day = start_date
    while day <= end_date:
        native = reference_intervals(configuration, day, now) if configuration else ()
        cursor, day_end = local_day_bounds(day, zone)
        available_count = recorded_count = missing_count = 0
        while cursor < day_end:
            end = min(cursor + timedelta(minutes=resolution_minutes), day_end)
            overlaps = [row for row in observations if row.starts_at < end and row.ends_at > cursor]
            exact = next(
                (row for row in overlaps if row.starts_at == cursor and row.ends_at == end), None
            )
            available = interval_is_covered(native, cursor, end) and end <= now
            reason = (
                "already_recorded"
                if exact
                else "observation_overlap"
                if overlaps
                else "configuration_unavailable"
                if configuration is None
                else "configuration_not_current"
                if not current
                else "estimated_actuals_unavailable"
                if not available
                else None
            )
            intervals.append(
                {
                    "starts_at": cursor,
                    "ends_at": end,
                    "reference_available": available,
                    "observation": encoded[exact.pk] if exact else None,
                    "can_enter": reason is None,
                    "unavailable_reason": reason,
                }
            )
            available_count += available
            recorded_count += bool(overlaps)
            missing_count += available and not overlaps
            cursor = end
        days.append(
            {
                "date": day,
                "available_count": available_count,
                "recorded_count": recorded_count,
                "missing_count": missing_count,
            }
        )
        day += timedelta(days=1)
    return {
        "plant_id": plant.pk,
        "configuration_id": configuration.pk if configuration else None,
        "timezone": zone,
        "available_start_date": first,
        "available_end_date": last,
        "start_date": start_date,
        "end_date": end_date,
        "resolution_minutes": resolution_minutes,
        "days": days,
        "intervals": intervals,
        "observations": list(encoded.values()),
    }
