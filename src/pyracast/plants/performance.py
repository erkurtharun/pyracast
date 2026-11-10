from datetime import date, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone

from pyracast.forecasts.models import ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecasting import calibration_at, power_intervals, power_limit
from pyracast.plants.measurements import Measurement, select_measurements
from pyracast.plants.models import Plant, PlantConfiguration
from pyracast.plants.numerics import (
    CalibrationSample,
    PowerInterval,
    RequiredWindows,
    evaluate_samples,
)
from pyracast.solar import DAYLIGHT_POLICY, daylight_window

SELECTION_POLICY = "published-day-ahead-replay-v1"


def _snapshot(configuration: PlantConfiguration, day: date) -> ForecastSnapshot | None:
    prior, start = local_day_bounds(day - timedelta(days=1), configuration.binding.timezone)
    return (
        ForecastSnapshot.objects.filter(
            job__binding_id=configuration.binding_id,
            job__status="succeeded",
            job__target_date=day,
            source_kind="forecast",
            collected_at__gte=prior,
            collected_at__lt=start,
            available_at__lt=start,
        )
        .filter(Q(issued_at__isnull=True) | Q(issued_at__lt=start))
        .order_by("collected_at", "pk")
        .first()
    )


def _samples(
    observations: list[Measurement],
    intervals: tuple[PowerInterval, ...],
    factor: float,
    cap: float | None,
) -> tuple[CalibrationSample, ...]:
    projected = tuple(
        PowerInterval(
            interval.start,
            interval.end,
            None
            if interval.power_mw is None
            else interval.power_mw * factor
            if cap is None
            else min(cap, interval.power_mw * factor),
        )
        for interval in intervals
    )
    return tuple(
        CalibrationSample(
            row.starts_at,
            row.ends_at,
            row.energy_mwh,
            tuple(
                interval
                for interval in projected
                if interval.start < row.ends_at and interval.end > row.starts_at
            ),
            row.quality,
        )
        for row in observations
    )


def _day(
    configuration: PlantConfiguration,
    day: date,
    observations: list[Measurement],
    required_windows: RequiredWindows | None,
) -> tuple[dict[str, Any], tuple[CalibrationSample, ...], tuple[CalibrationSample, ...]]:
    result: dict[str, Any] = {
        "date": day,
        "status": "missing",
        "reason": None,
        "snapshot_id": None,
        "collected_at": None,
        "available_at": None,
        "calibration_id": None,
        "factor": None,
        "measured_energy_mwh": None,
        "predicted_energy_mwh": None,
        "baseline_energy_mwh": None,
        "error_mwh": None,
        "manual_observation_ids": [],
        "settlement_snapshot_ids": [],
    }
    zone = configuration.binding.timezone
    start, end = local_day_bounds(day, zone)
    if configuration.created_at >= start:
        result["reason"] = "configuration_not_available"
        return result, (), ()
    snapshot = _snapshot(configuration, day)
    if snapshot is None:
        result["reason"] = "forecast_not_available"
        return result, (), ()
    calibration = calibration_at(configuration, start - timedelta(microseconds=1))
    factor = cast(float, calibration.factor) if calibration is not None else 1.0
    result.update(
        snapshot_id=snapshot.pk,
        collected_at=snapshot.collected_at,
        available_at=snapshot.available_at,
        calibration_id=calibration.pk if calibration else None,
        factor=factor,
    )
    readings = [row for row in observations if row.starts_at >= start and row.ends_at <= end]
    try:
        intervals = power_intervals(snapshot, configuration)
        cap = power_limit(configuration)
        selected = _samples(readings, intervals, factor, cap)
        baseline = _samples(readings, intervals, 1.0, cap)
        evaluation = evaluate_samples(
            selected, timezone=zone, limit_mw=None, required_windows=required_windows
        )
        reference = evaluate_samples(
            baseline, timezone=zone, limit_mw=None, required_windows=required_windows
        )
    except ValueError, ArithmeticError:
        result["reason"] = "invalid_forecast_data"
        return result, (), ()
    if not evaluation["valid_days"] or not reference["valid_days"]:
        result["reason"] = "incomplete_or_invalid_day"
        return result, (), ()
    errors = cast(list[dict[str, Any]], evaluation["day_errors"])
    reference_errors = cast(list[dict[str, Any]], reference["day_errors"])
    used_readings = [
        row
        for row in readings
        if required_windows is None
        or any(
            row.starts_at < stop and row.ends_at > begin
            for begin, stop in required_windows.get(day, ())
        )
    ]
    result.update(
        status="matched",
        measured_energy_mwh=errors[0]["measured_energy_mwh"],
        predicted_energy_mwh=errors[0]["predicted_energy_mwh"],
        baseline_energy_mwh=reference_errors[0]["predicted_energy_mwh"],
        error_mwh=errors[0]["error_mwh"],
        manual_observation_ids=sorted(
            {row.pk for row in used_readings if row.snapshot is None and row.pk is not None}
        ),
        settlement_snapshot_ids=sorted(
            {row.snapshot.pk for row in used_readings if row.snapshot is not None}
        ),
    )
    return result, selected, baseline


def forecast_performance(
    plant: Plant,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    daylight_only: bool = False,
) -> dict[str, Any]:
    configuration = (
        PlantConfiguration.objects.select_related("binding").get(
            pk=plant.current_configuration_id, plant=plant
        )
        if plant.current_configuration_id is not None
        else None
    )
    zone = configuration.binding.timezone if configuration else plant.region.timezone
    now = timezone.now()
    today = now.astimezone(ZoneInfo(zone)).date()
    if (start_date is None) != (end_date is None):
        raise ValidationError("Both start_date and end_date are required.")
    if start_date is None or end_date is None:
        end_date = today - timedelta(days=1)
        start_date = end_date - timedelta(days=29)
    if (
        not 1 <= (end_date - start_date).days + 1 <= 90
        or start_date < today - timedelta(days=366)
        or end_date >= today
    ):
        raise ValidationError("Use 1 to 90 completed local days within the past year.")
    start, _ = local_day_bounds(start_date, zone)
    _, end = local_day_bounds(end_date, zone)
    observations = select_measurements(configuration, start, end, now)[0] if configuration else []
    days: list[dict[str, Any]] = []
    selected: tuple[CalibrationSample, ...] = ()
    baseline: tuple[CalibrationSample, ...] = ()
    required_windows = None
    if daylight_only:
        location = configuration.binding if configuration else plant.region
        required_windows = {
            start_date + timedelta(days=offset): daylight_window(
                start_date + timedelta(days=offset), zone, location.latitude, location.longitude
            ).windows
            for offset in range((end_date - start_date).days + 1)
        }
    if configuration is not None:
        for offset in range((end_date - start_date).days + 1):
            day, chosen, reference = _day(
                configuration, start_date + timedelta(days=offset), observations, required_windows
            )
            days.append(day)
            selected += chosen
            baseline += reference
    evaluation = evaluate_samples(
        selected, timezone=zone, limit_mw=None, required_windows=required_windows
    )
    comparison = evaluate_samples(
        baseline, timezone=zone, limit_mw=None, required_windows=required_windows
    )
    return {
        "plant_id": plant.pk,
        "configuration_id": configuration.pk if configuration else None,
        "timezone": zone,
        "start_date": start_date,
        "end_date": end_date,
        "evaluated_at": now,
        "evaluation_kind": "historical_input_replay",
        "selection_policy": SELECTION_POLICY,
        "measurement_basis": configuration.measurement_basis if configuration else None,
        "metric_scope": "daylight_intervals" if daylight_only else "full_day",
        "daylight_policy": DAYLIGHT_POLICY if daylight_only else None,
        "matched_days": evaluation["valid_days"],
        "requested_days": (end_date - start_date).days + 1,
        "metrics": evaluation["metrics"],
        "baseline_metrics": comparison["metrics"],
        "days": days,
        "uncertainty": "not_estimated",
    }
