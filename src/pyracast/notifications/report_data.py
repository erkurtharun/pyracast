from datetime import date, datetime, timedelta
from typing import TypedDict, cast
from zoneinfo import ZoneInfo

from pyracast.forecasts.models import ForecastSnapshot
from pyracast.incidents.operations import pinned_incidents
from pyracast.irradiance import local_day_bounds
from pyracast.notifications.models import NotificationDelivery
from pyracast.plants.forecasting import (
    calibration_at,
    forecast_daylight,
    forecast_payload,
    power_intervals,
    power_limit,
)
from pyracast.plants.incident_forecasts import incident_scenario
from pyracast.plants.measurements import production_identity_at
from pyracast.plants.models import Plant, PlantConfiguration
from pyracast.plants.numerics import PowerInterval, forecast
from pyracast.plants.operating_limits import OperatingLimit, affected_power


class ReportDay(TypedDict):
    date: str
    normal: dict[str, object] | None
    outage: dict[str, object] | None
    outage_status: str | None
    incident_revision_ids: list[int]


class ReportContext(TypedDict):
    plant_id: int | None
    plant_name: str
    configuration_id: int | None
    timezone: str
    collected_at: str | None
    days: list[ReportDay]


def _report_intervals(
    configuration: PlantConfiguration,
    snapshot: ForecastSnapshot,
    day: date,
    normal: dict[str, object],
    revision_ids: list[int],
) -> list[dict[str, object]]:
    daylight, windows = forecast_daylight(configuration, day, snapshot.normalized_intervals)
    start, end = local_day_bounds(day, configuration.binding.timezone)
    limits = tuple(
        OperatingLimit(
            row.starts_at,
            row.ends_at,
            row.available_dc_mw if row.declared_configuration_id == configuration.pk else None,
            row.available_inverter_mw
            if row.declared_configuration_id == configuration.pk
            else None,
            row.available_export_mw if row.declared_configuration_id == configuration.pk else None,
        )
        for row in pinned_incidents(configuration.plant_id, revision_ids)
    )
    intervals = affected_power(
        power_intervals(snapshot, configuration, model_version=cast(str, normal["model_version"])),
        limits,
        dc_capacity_mw=configuration.dc_capacity_mw,
        normal_limit_mw=power_limit(configuration),
        factor=cast(float, normal["factor"]),
        net_export=configuration.measurement_basis == "net_export",
    )
    scoped = tuple(
        PowerInterval(max(row.start, left), min(row.end, right), row.power_mw)
        for row in intervals
        for left, right in windows
        if row.start < right and row.end > left
    )
    scope: list[tuple[datetime, datetime]] = []
    for left, right in sorted((*daylight.windows, *windows)):
        left, right = max(start, left), min(end, right)
        if scope and left <= scope[-1][1]:
            scope[-1] = (scope[-1][0], max(right, scope[-1][1]))
        else:
            scope.append((left, right))
    result: list[dict[str, object]] = []
    cursor = start
    while cursor < end:
        stop = min(cursor + timedelta(hours=1), end)
        for left, right in scope:
            begin, finish = max(cursor, left), min(stop, right)
            if begin < finish:
                payload = forecast(
                    scoped, start=begin, end=finish, resolution_minutes=60, limit_mw=None
                )
                result.extend(cast(list[dict[str, object]], payload["intervals"]))
        cursor = stop
    return result


def saved_report_context(
    delivery: NotificationDelivery, *, known_at: datetime | None = None
) -> dict[str, object]:
    binding = delivery.binding
    assert binding is not None  # noqa: S101 -- Preparation requires an authorized binding.
    cutoff = known_at or delivery.scheduled_for
    today = delivery.scheduled_for.astimezone(ZoneInfo(binding.timezone)).date()
    plant = Plant.objects.filter(
        region_id=delivery.schedule.region_id,
        region__tenant_id=binding.region.tenant_id,
        deleted_at__isnull=True,
    ).first()
    configuration = (
        PlantConfiguration.objects.select_related("binding", "plant")
        .filter(
            pk=plant.current_configuration_id,
            plant=plant,
            binding=binding,
            created_at__lte=cutoff,
        )
        .first()
        if plant is not None and plant.current_configuration_id is not None
        else None
    )
    context = ReportContext(
        plant_id=plant.pk if plant else None,
        plant_name=plant.name if plant else delivery.schedule.region.name,
        configuration_id=configuration.pk if configuration else None,
        timezone=binding.timezone,
        collected_at=delivery.snapshot.collected_at.isoformat() if delivery.snapshot else None,
        days=[],
    )
    calibration = calibration_at(configuration, cutoff) if configuration else None
    if calibration is not None and not production_identity_at(calibration, cutoff):
        calibration = None
    snapshot = delivery.snapshot
    for offset in (0, 1):
        day = today + timedelta(days=offset)
        row = ReportDay(
            date=day.isoformat(),
            normal=None,
            outage=None,
            outage_status=None,
            incident_revision_ids=[],
        )
        if (
            configuration is not None
            and snapshot is not None
            and snapshot.binding_id == binding.pk
            and snapshot.source_kind == "forecast"
        ):
            try:
                normal = forecast_payload(configuration, snapshot, calibration, day, 60)
                outlook: dict[str, object] = {
                    "plant_id": configuration.plant_id,
                    "configuration_id": configuration.pk,
                    "calibration_id": calibration.pk if calibration else None,
                    "result": normal,
                }
                scenario = incident_scenario(outlook, day, 60, cutoff)
                normal = cast(dict[str, object], outlook["result"])
                normal["report_intervals"] = _report_intervals(
                    configuration, snapshot, day, normal, []
                )
                if scenario is not None:
                    outage = cast(dict[str, object], scenario["result"])
                    revision_ids = cast(list[int], scenario["incident_revision_ids"])
                    outage["report_intervals"] = _report_intervals(
                        configuration, snapshot, day, normal, revision_ids
                    )
                    row["outage"] = outage
                    row["outage_status"] = cast(str, scenario["status"])
                    row["incident_revision_ids"] = revision_ids
                row["normal"] = normal
            except ValueError, ArithmeticError, KeyError, TypeError:
                # A damaged acquisition cannot become a plausible electrical estimate.
                pass
        context["days"].append(row)
    return cast(dict[str, object], context)
