from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from typing import Any, cast
from zoneinfo import ZoneInfo

from django.db.models import F, Q

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecasting import (
    calibration_at,
    forecast_payload,
    power_intervals,
    power_limit,
)
from pyracast.plants.incident_forecasts import incident_scenario
from pyracast.plants.measurements import production_identity_at
from pyracast.plants.models import ForecastIssuance, Plant, PlantConfiguration
from pyracast.plants.numerics import PowerInterval
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
    native_intervals: list[dict[str, object]]


def forecast_cutoff(day: date, binding: SiteBinding) -> datetime:
    daylight = daylight_window(day, binding.timezone, binding.latitude, binding.longitude)
    start, _ = local_day_bounds(day, binding.timezone)
    return daylight.windows[0][0] if daylight.windows else start


def _native_intervals(
    intervals: tuple[PowerInterval, ...],
    start: datetime,
    end: datetime,
    factor: float,
    limit: float | None,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for interval in intervals:
        begin, stop = max(start, interval.start), min(end, interval.end)
        if begin >= stop:
            continue
        power = interval.power_mw
        calibrated = power * factor if power is not None else None
        rows.append(
            {
                "start": begin.isoformat(),
                "end": stop.isoformat(),
                "original_start": interval.start.isoformat(),
                "original_end": interval.end.isoformat(),
                "power_mw": min(calibrated, limit)
                if calibrated is not None and limit is not None
                else calibrated,
                "baseline_power_mw": min(power, limit)
                if power is not None and limit is not None
                else power,
            }
        )
    return rows


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
        for row in snapshot.normalized_intervals
    ):
        return ()
    calibration = calibration_at(configuration, snapshot.collected_at)
    if calibration is not None and not production_identity_at(calibration, snapshot.collected_at):
        calibration = None
    factor = float(calibration.factor) if calibration and calibration.factor is not None else 1.0
    native = tuple(sorted(power_intervals(snapshot, configuration), key=lambda row: row.start))
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
            str(resolution): forecast_payload(configuration, snapshot, calibration, day, resolution)
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
            )
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
                native_intervals=_native_intervals(
                    native, start, end, factor, power_limit(configuration)
                ),
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
                "native_intervals": item.native_intervals,
            },
        )


def select_forecast_issuance(
    plant: Plant,
    day: date,
    *,
    as_of: datetime | None = None,
    binding_id: int | None = None,
    native_only: bool = False,
) -> ForecastIssuance | None:
    return select_forecast_issuances(
        plant, {day: as_of}, binding_id=binding_id, native_only=native_only
    ).get(day)


def select_forecast_issuances(
    plant: Plant,
    cutoffs: Mapping[date, datetime | None],
    *,
    binding_id: int | None = None,
    native_only: bool = False,
) -> dict[date, ForecastIssuance]:
    issuances = (
        ForecastIssuance.objects.select_related(
            "configuration__binding", "snapshot__job", "calibration"
        )
        .defer("snapshot__raw_payload", "snapshot__weather_payload")
        .filter(plant=plant, target_date__in=tuple(cutoffs))
    )
    if native_only:
        issuances = issuances.defer(
            "payloads",
            "outage_scenarios",
            "snapshot__normalized_intervals",
            "calibration__dataset_manifest",
            "calibration__result",
        )
    if any(cutoff is not None for cutoff in cutoffs.values()):
        available = Q(pk__in=[])
        for day, cutoff in cutoffs.items():
            eligible = Q(target_date=day)
            if cutoff is not None:
                if cutoff.utcoffset() is None:
                    raise ValueError("The selection cutoff must be timezone-aware.")
                eligible &= Q(published_at__lte=cutoff, snapshot__collected_at__lte=cutoff)
                eligible &= Q(snapshot__issued_at__isnull=True) | Q(snapshot__issued_at__lte=cutoff)
            available |= eligible
        issuances = issuances.filter(available)
    if binding_id is not None:
        issuances = issuances.filter(snapshot__binding_id=binding_id)
    return {
        issuance.target_date: issuance
        for issuance in issuances.order_by(
            "target_date", "-snapshot__collected_at", "-pk"
        ).distinct("target_date")
    }


def issuance_power_intervals(
    issuance: ForecastIssuance, *, baseline: bool = False
) -> tuple[PowerInterval, ...]:
    rows = issuance.native_intervals
    if not isinstance(rows, list):
        raise ValueError("Saved issuance intervals must be a list.")
    intervals = []
    previous: datetime | None = None
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("A saved issuance interval must be an object.")
        start, end = row.get("start"), row.get("end")
        if not isinstance(start, str) or not isinstance(end, str):
            raise ValueError("Saved issuance timestamps must be ISO strings.")
        begin, stop = datetime.fromisoformat(start), datetime.fromisoformat(end)
        if begin.utcoffset() is None or stop.utcoffset() is None:
            raise ValueError("Saved issuance timestamps must be timezone-aware.")
        begin, stop = begin.astimezone(UTC), stop.astimezone(UTC)
        if stop <= begin or (previous is not None and begin < previous):
            raise ValueError("Saved issuance intervals must be ordered and non-overlapping.")
        power: Any = row.get("baseline_power_mw" if baseline else "power_mw")
        if power is not None and (
            isinstance(power, bool)
            or not isinstance(power, int | float)
            or not isfinite(power)
            or power < 0
        ):
            raise ValueError("Saved issuance power must be finite and nonnegative, or missing.")
        intervals.append(PowerInterval(begin, stop, power))
        previous = stop
    return tuple(intervals)
