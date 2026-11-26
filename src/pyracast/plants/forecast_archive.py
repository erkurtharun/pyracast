from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from django.db.models import F, OuterRef

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.forecasts.sources import snapshot_rows
from pyracast.forecasts.timing import approval_job_id
from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecasting import (
    calibration_at,
    forecast_payload,
    power_intervals,
)
from pyracast.plants.incident_forecasts import incident_scenario
from pyracast.plants.measurements import production_identity_at
from pyracast.plants.models import ForecastIssuance, Plant, PlantConfiguration
from pyracast.plants.transfer import transfer_model
from pyracast.solar import daylight_window

ARCHIVE_SELECTION_POLICY = "latest-published-pre-daylight-issuance-v1"


@dataclass(frozen=True)
class PreparedForecastIssuance:
    plant_id: int
    configuration_id: int
    snapshot_id: int
    binding_id: int
    collected_at: datetime
    issued_at: datetime | None
    target_date: date
    cutoff_at: datetime
    calibration_id: int | None
    model_version: str
    factor: float
    payloads: dict[str, dict[str, object]]
    outage_scenarios: dict[str, dict[str, object] | None]


def forecast_cutoff(day: date, binding: SiteBinding) -> datetime:
    daylight = daylight_window(day, binding.timezone, binding.latitude, binding.longitude)
    start, _ = local_day_bounds(day, binding.timezone)
    return daylight.windows[0][0] if daylight.windows else start


def prepare_forecast_issuances(snapshot: ForecastSnapshot) -> tuple[PreparedForecastIssuance, ...]:
    if snapshot.source_kind != "forecast" or (
        snapshot.issued_at is not None and snapshot.issued_at > snapshot.collected_at
    ):
        return ()
    configuration = (
        PlantConfiguration.objects.select_related("binding", "plant")
        .filter(
            plant__region_id=snapshot.binding.region_id,
            plant__current_configuration_id=F("pk"),
        )
        .first()
    )
    return _prepare_for_configuration(snapshot, configuration)


def _prepare_for_configuration(
    snapshot: ForecastSnapshot, configuration: PlantConfiguration | None
) -> tuple[PreparedForecastIssuance, ...]:
    if (
        configuration is None
        or configuration.binding_id != snapshot.binding_id
        or configuration.created_at > snapshot.collected_at
    ):
        return ()
    if any(
        row.get("source_kind", "forecast") != "forecast" or row.get("source_kinds")
        for row in snapshot_rows(snapshot)
    ):
        return ()
    calibration = calibration_at(configuration, snapshot.collected_at)
    if calibration is not None and not production_identity_at(calibration, snapshot.collected_at):
        calibration = None
    factor = float(calibration.factor) if calibration and calibration.factor is not None else 1.0
    model = transfer_model(configuration, calibration)
    native = power_intervals(snapshot, configuration, model_version=model)
    binding = configuration.binding
    first = snapshot.collected_at.astimezone(ZoneInfo(binding.timezone)).date()
    prepared = []
    for offset in range(3):
        day = first + timedelta(days=offset)
        start, end = local_day_bounds(day, binding.timezone)
        if not any(row.start < end and row.end > start for row in native):
            continue
        cutoff = forecast_cutoff(day, binding)
        if snapshot.collected_at >= cutoff or (
            snapshot.issued_at is not None and snapshot.issued_at >= cutoff
        ):
            continue
        payloads = {
            str(resolution): forecast_payload(
                configuration, snapshot, calibration, day, resolution, model_version=model
            )
            for resolution in (30, 60)
        }
        scenarios = {
            resolution: incident_scenario(
                {
                    "plant_id": configuration.plant_id,
                    "configuration_id": configuration.pk,
                    "calibration_id": calibration.pk if calibration else None,
                    "result": deepcopy(payload),
                },
                day,
                int(resolution),
                snapshot.collected_at,
            ).scenario
            for resolution, payload in payloads.items()
        }
        prepared.append(
            PreparedForecastIssuance(
                plant_id=configuration.plant_id,
                configuration_id=configuration.pk,
                snapshot_id=snapshot.pk,
                binding_id=binding.pk,
                collected_at=snapshot.collected_at,
                issued_at=snapshot.issued_at,
                target_date=day,
                cutoff_at=cutoff,
                calibration_id=calibration.pk if calibration else None,
                model_version=cast(str, payloads["30"]["model_version"]),
                factor=factor,
                payloads=payloads,
                outage_scenarios=scenarios,
            )
        )
    return tuple(prepared)


def save_forecast_issuances(
    prepared: tuple[PreparedForecastIssuance, ...],
    snapshot: ForecastSnapshot,
    published_at: datetime,
    *,
    plant: Plant | None,
) -> None:
    """The publisher owns the transaction and region/job/plant/account locks."""
    if published_at.utcoffset() is None:
        raise ValueError("Publication time must be timezone-aware.")
    for item in prepared:
        if (
            plant is None
            or plant.pk != item.plant_id
            or plant.current_configuration_id != item.configuration_id
        ):
            continue
        if (
            snapshot.pk != item.snapshot_id
            or snapshot.binding_id != item.binding_id
            or snapshot.source_kind != "forecast"
            or snapshot.collected_at != item.collected_at
            or snapshot.issued_at != item.issued_at
        ):
            raise ValueError("Prepared issuance and acquisition must match.")
        if not item.collected_at <= published_at < item.cutoff_at or (
            item.issued_at is not None and item.issued_at > item.collected_at
        ):
            continue
        payloads = deepcopy(item.payloads)
        scenarios = deepcopy(item.outage_scenarios)
        outputs = list(payloads.values())
        for scenario in scenarios.values():
            if scenario is None:
                continue
            outputs.append(cast(dict[str, object], scenario["result"]))
            for incident in cast(list[dict[str, object]], scenario["incidents"]):
                for field in ("recorded_at", "starts_at", "ends_at", "cancelled_at"):
                    stamp = cast(datetime | None, incident[field])
                    incident[field] = stamp.isoformat() if stamp is not None else None
        for payload in outputs:
            provenance = cast(dict[str, object], payload["provenance"])
            provenance.update(
                selection_policy=ARCHIVE_SELECTION_POLICY,
                collected_at=item.collected_at.isoformat(),
                published_at=published_at.isoformat(),
                selection_cutoff=item.cutoff_at.isoformat(),
                calibration_id=item.calibration_id,
                model_version=item.model_version,
                factor=item.factor,
            )
        ForecastIssuance.objects.get_or_create(
            plant_id=item.plant_id,
            snapshot=snapshot,
            target_date=item.target_date,
            defaults={
                "configuration_id": item.configuration_id,
                "calibration_id": item.calibration_id,
                "published_at": published_at,
                "cutoff_at": item.cutoff_at,
                "model_version": item.model_version,
                "factor": item.factor,
                "payloads": payloads,
                "outage_scenarios": scenarios,
            },
        )


def select_forecast_issuance(
    plant: Plant, day: date, *, as_of: datetime | None = None, binding_id: int | None = None
) -> ForecastIssuance | None:
    """The latest collected forecast saved for the day, as known at `as_of`."""
    issuances = ForecastIssuance.objects.select_related(
        "configuration__binding", "snapshot__job", "calibration"
    ).filter(plant=plant, target_date=day)
    if as_of is not None:
        if as_of.utcoffset() is None:
            raise ValueError("The selection cutoff must be timezone-aware.")
        issuances = issuances.filter(published_at__lte=as_of, snapshot__collected_at__lte=as_of)
        issuances = issuances.exclude(snapshot__issued_at__gt=as_of)
    if binding_id is not None:
        issuances = issuances.filter(snapshot__binding_id=binding_id)
    return (
        issuances.defer("snapshot__raw_payload").order_by("-snapshot__collected_at", "-pk").first()
    )


def saved_issuances(
    plant: Plant, first: date, last: date, now: datetime
) -> dict[date, list[tuple[ForecastIssuance, bool]]]:
    """By day from `first` to `last`: every forecast the plant saved for it, newest fetch first,
    each with whether it is the fetch of the job approval made first."""
    rows = (
        ForecastIssuance.objects.select_related("configuration__binding", "snapshot__binding")
        .defer("snapshot__raw_payload", "snapshot__normalized_intervals")
        .filter(plant=plant, target_date__range=(first, last), published_at__lte=now)
        .annotate(approval_job=approval_job_id(OuterRef("snapshot__binding_id")))
        .order_by("target_date", "-snapshot__collected_at", "-pk")
    )
    days: defaultdict[date, list[tuple[ForecastIssuance, bool]]] = defaultdict(list)
    for row in rows:
        days[row.target_date].append((row, row.snapshot.job_id == row.approval_job))
    return dict(days)
