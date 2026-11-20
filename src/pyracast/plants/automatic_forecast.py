from datetime import date, datetime
from typing import cast
from zoneinfo import ZoneInfo

from pyracast.forecasts.coverage import history_day_complete
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from pyracast.plants.current_forecast import current_forecast
from pyracast.plants.forecasting import archived_forecast
from pyracast.plants.models import CONFIGURATION_PARAMETERS, Plant, PlantConfiguration


def automatic_forecast(
    plant: Plant, target_date: date, resolution_minutes: int, now: datetime
) -> dict[str, object]:
    today = now.astimezone(ZoneInfo(plant.region.timezone)).date()
    latest = current_forecast(
        plant,
        target_date,
        resolution_minutes,
        now,
        actuals="elapsed" if target_date == today else "none",
    )
    return reference_transition_forecast(plant, target_date, resolution_minutes, now, latest)


def reference_transition_forecast(
    plant: Plant,
    target_date: date,
    resolution_minutes: int,
    now: datetime,
    latest: dict[str, object],
) -> dict[str, object]:
    payload = cast(dict[str, object] | None, latest["result"])
    if (
        target_date != now.astimezone(ZoneInfo(plant.region.timezone)).date()
        or latest["source_kind"] != "forecast"
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
    reference_fields = (
        "region_id",
        "provider_account_id",
        "site_id",
        "latitude",
        "longitude",
        "timezone",
        "azimuth_degrees",
        "losses_percent",
        "power_basis",
    )
    if (
        previous.binding.capacity_kw == current.binding.capacity_kw
        or not previous.binding.capacity_semantics_verified
        or previous.binding.health != "healthy"
        or any(
            getattr(previous, field) != getattr(current, field)
            for field in CONFIGURATION_PARAMETERS
        )
        or any(
            getattr(previous.binding, field) != getattr(current.binding, field)
            for field in reference_fields
        )
    ):
        return latest
    archived = archived_forecast(
        plant, target_date, resolution_minutes, now, binding_id=previous.binding_id
    )
    archived_payload = cast(dict[str, object] | None, archived["result"])
    if (
        archived["configuration_id"] != previous.pk
        or archived_payload is None
        or archived_payload["complete"] is not True
    ):
        return latest
    provenance = cast(dict[str, object], archived_payload["provenance"])
    snapshot = ForecastSnapshot.objects.filter(
        pk=cast(int, provenance["snapshot_id"]), job__binding=previous.binding
    ).first()
    if snapshot is None or not history_day_complete(
        snapshot, target_date, previous.binding.timezone
    ):
        return latest
    cast(list[str], archived_payload["quality_flags"]).append("reference_transition_fallback")
    provenance.update(
        automatic_selection_policy="complete-day-ahead-on-reference-transition-v1",
        current_configuration_id=current.pk,
        archived_selection_cutoff=archived["as_of"],
    )
    archived.pop("outage_scenario", None)
    archived["as_of"] = now.isoformat()
    return archived
