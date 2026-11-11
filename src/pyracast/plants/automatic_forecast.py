from datetime import date, datetime
from typing import cast
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError

from pyracast.forecasts.coverage import history_day_complete
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from pyracast.plants.current_forecast import current_forecast
from pyracast.plants.forecasting import archived_forecast
from pyracast.plants.models import Plant, PlantConfiguration


def automatic_forecast(
    plant: Plant, target_date: date, resolution_minutes: int, now: datetime
) -> dict[str, object]:
    latest = current_forecast(plant, target_date, resolution_minutes, now)
    payload = cast(dict[str, object] | None, latest["result"])
    if (
        target_date != now.astimezone(ZoneInfo(plant.region.timezone)).date()
        or latest["configuration_id"] is None
        or (payload is not None and payload["complete"] is True)
    ):
        return latest
    current = PlantConfiguration.objects.select_related("binding").get(
        pk=cast(int, latest["configuration_id"]), plant=plant
    )
    start, _ = local_day_bounds(target_date, current.binding.timezone)
    if current.created_at <= start:
        return latest
    if payload is not None:
        sources = cast(dict[str, object], payload["provenance"])
        segments = cast(list[dict[str, object]], sources["source_segments"])
        if any(datetime.fromisoformat(cast(str, row["start"])) <= start for row in segments):
            return latest
    previous = (
        PlantConfiguration.objects.select_related("binding")
        .filter(plant=plant, created_at__lte=start)
        .order_by("-created_at", "-pk")
        .first()
    )
    if previous is None:
        return latest
    physical_fields = (
        "dc_capacity_mw",
        "inverter_capacity_mw",
        "export_limit_mw",
        "measurement_basis",
        "mounting_type",
        "surface_tilt",
        "surface_azimuth",
    )
    reference_fields = (
        "region_id",
        "provider_account_id",
        "site_id",
        "latitude",
        "longitude",
        "timezone",
        "tilt_degrees",
        "azimuth_degrees",
        "losses_percent",
        "power_basis",
        "capacity_basis",
    )
    if (
        previous.binding.capacity_kw == current.binding.capacity_kw
        or not previous.binding.capacity_semantics_verified
        or previous.binding.health != "healthy"
        or any(getattr(previous, field) != getattr(current, field) for field in physical_fields)
        or any(
            getattr(previous.binding, field) != getattr(current.binding, field)
            for field in reference_fields
        )
    ):
        return latest
    try:
        archived = archived_forecast(plant, target_date, resolution_minutes, now)
    except ValidationError:
        return latest
    archived_payload = cast(dict[str, object] | None, archived["result"])
    if archived_payload is None or archived_payload["complete"] is not True:
        return latest
    provenance = cast(dict[str, object], archived_payload["provenance"])
    snapshot = ForecastSnapshot.objects.get(
        pk=cast(int, provenance["snapshot_id"]), job__binding=previous.binding
    )
    if not history_day_complete(snapshot, target_date, previous.binding.timezone):
        return latest
    cast(list[str], archived_payload["quality_flags"]).append("reference_transition_fallback")
    provenance.update(
        automatic_selection_policy="complete-day-ahead-on-reference-transition-v1",
        current_configuration_id=current.pk,
        archived_selection_cutoff=archived["as_of"],
    )
    archived["as_of"] = now.isoformat()
    return archived
