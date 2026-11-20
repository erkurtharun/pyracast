from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext

from pyracast.forecasts.actuals import ACTUAL_COMPOSITION_POLICY, select_archived_actual_source
from pyracast.forecasts.history import HISTORY_SELECTION_POLICY
from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecast_archive import (
    ARCHIVE_SELECTION_POLICY,
    issuance_power_intervals,
    select_forecast_issuances,
)
from pyracast.plants.forecasting import (
    calibration_for_day,
    power_intervals,
    power_limit,
)
from pyracast.plants.measurement_policy import LEGACY_MEASUREMENT_POLICY, MEASUREMENT_POLICY
from pyracast.plants.measurements import Measurement, select_measurements
from pyracast.plants.models import ForecastIssuance, Plant, PlantConfiguration
from pyracast.plants.numerics import (
    CalibrationSample,
    PowerInterval,
    RequiredWindows,
    evaluate_samples,
)
from pyracast.plants.transfer import transfer_model
from pyracast.solar import DAYLIGHT_POLICY, daylight_window


def _selection_cutoff(day: date, zone: str, issuance_cutoff: time) -> datetime:
    local = datetime.combine(day - timedelta(days=1), issuance_cutoff, ZoneInfo(zone))
    cutoff = local.astimezone(UTC)
    if (
        cutoff.astimezone(ZoneInfo(zone)).replace(tzinfo=None) != local.replace(tzinfo=None)
        or local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset()
    ):
        raise ValidationError(gettext("The local issuance cutoff is ambiguous or does not exist."))
    return cutoff


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
    comparison: str,
    cutoff: datetime,
    selection_cutoff: datetime | None,
    issuance: ForecastIssuance | None,
    configuration_comparable: bool,
) -> tuple[dict[str, Any], tuple[CalibrationSample, ...], tuple[CalibrationSample, ...]]:
    result: dict[str, Any] = {
        "date": day,
        "status": "missing",
        "reason": None,
        "snapshot_id": None,
        "issuance_id": issuance.pk if issuance is not None else None,
        "configuration_id": configuration.pk,
        "model_version": issuance.model_version if issuance is not None else None,
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
        "provisional_snapshot_ids": [],
        "source_kind": None,
        "historical_configuration_assumed": False,
        "provisional_production": False,
        "selection_cutoff": selection_cutoff,
    }
    zone = configuration.binding.timezone
    start, end = local_day_bounds(day, zone)
    historical_model = comparison == "historical_model"
    if not configuration_comparable:
        result["reason"] = "configuration_not_comparable"
        return result, (), ()
    actual_source = (
        select_archived_actual_source(configuration.binding, day, cutoff)
        if historical_model
        else None
    )
    snapshot = (
        (actual_source.snapshot if actual_source is not None else None)
        if historical_model
        else issuance.snapshot
        if issuance is not None
        else None
    )
    if snapshot is None:
        result["reason"] = (
            "estimated_actuals_not_available" if historical_model else "forecast_not_available"
        )
        return result, (), ()
    calibration = (
        calibration_for_day(configuration, day, cutoff)
        if historical_model
        else issuance.calibration
        if issuance is not None
        else None
    )
    factor = (
        issuance.factor
        if issuance is not None
        else cast(float, calibration.factor)
        if calibration is not None
        else 1.0
    )
    result.update(
        snapshot_id=snapshot.pk,
        collected_at=snapshot.collected_at,
        available_at=issuance.published_at if issuance is not None else snapshot.available_at,
        calibration_id=calibration.pk if calibration else None,
        factor=factor,
        source_kind=snapshot.source_kind,
        historical_configuration_assumed=historical_model and configuration.created_at >= start,
    )
    if actual_source is not None and actual_source.composition is not None:
        result["source_snapshot_ids"] = list(actual_source.composition.snapshot_ids)
        result["reference_provenance"] = {
            "selection_policy": ACTUAL_COMPOSITION_POLICY,
            "source_snapshot_ids": list(actual_source.composition.snapshot_ids),
            "source_segments": actual_source.composition.segments,
            "oldest_collected_at": min(
                row.collected_at for row in actual_source.snapshots
            ).isoformat(),
        }
    readings = [
        row
        for row in observations
        if row.starts_at >= start
        and row.ends_at <= end
        and (
            required_windows is None
            or any(
                row.starts_at < stop and row.ends_at > begin
                for begin, stop in required_windows.get(day, ())
            )
        )
    ]
    try:
        if issuance is not None:
            selected = _samples(readings, issuance_power_intervals(issuance), 1.0, None)
            baseline = _samples(
                readings, issuance_power_intervals(issuance, baseline=True), 1.0, None
            )
        else:
            intervals = power_intervals(
                snapshot,
                configuration,
                model_version=transfer_model(configuration, calibration),
                source_intervals=actual_source.composition.intervals
                if actual_source is not None and actual_source.composition is not None
                else None,
            )
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
    result.update(
        status="matched",
        measured_energy_mwh=errors[0]["measured_energy_mwh"],
        predicted_energy_mwh=errors[0]["predicted_energy_mwh"],
        baseline_energy_mwh=reference_errors[0]["predicted_energy_mwh"],
        error_mwh=errors[0]["error_mwh"],
        manual_observation_ids=sorted(
            {row.pk for row in readings if row.snapshot is None and row.pk is not None}
        ),
        settlement_snapshot_ids=sorted(
            {
                row.snapshot.pk
                for row in readings
                if row.snapshot is not None and row.source == "settlement_injection"
            }
        ),
        provisional_snapshot_ids=sorted(
            {
                row.snapshot.pk
                for row in readings
                if row.snapshot is not None and row.source == "realtime_generation"
            }
        ),
        provisional_production=any(row.source == "realtime_generation" for row in readings),
    )
    return result, selected, baseline


def forecast_performance(
    plant: Plant,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    daylight_only: bool = False,
    comparison: str = "archived_forecast",
    issuance_cutoff: time | None = None,
) -> dict[str, Any]:
    if comparison not in ("archived_forecast", "historical_model"):
        raise ValidationError(gettext("Unknown comparison mode."))
    if issuance_cutoff is not None:
        if issuance_cutoff.tzinfo is not None:
            raise ValidationError(gettext("Use a local issuance cutoff without a UTC offset."))
        if comparison != "archived_forecast":
            raise ValidationError(
                gettext("An issuance cutoff requires archived_forecast comparison.")
            )
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
        raise ValidationError(gettext("Both start_date and end_date are required."))
    if start_date is None or end_date is None:
        end_date = today - timedelta(days=1)
        start_date = end_date - timedelta(days=29)
    if (
        not 1 <= (end_date - start_date).days + 1 <= 90
        or start_date < today - timedelta(days=366)
        or end_date >= today
    ):
        raise ValidationError(gettext("Use 1 to 90 completed local days within the past year."))
    start, _ = local_day_bounds(start_date, zone)
    _, end = local_day_bounds(end_date, zone)
    measurement_policy = (
        MEASUREMENT_POLICY if comparison == "historical_model" else LEGACY_MEASUREMENT_POLICY
    )
    observations = (
        select_measurements(
            configuration,
            start,
            end,
            now,
            policy=measurement_policy,
            incident_revision_ids=[] if comparison == "archived_forecast" else None,
        )[0]
        if configuration
        else []
    )
    measurement_cache = {configuration.pk: observations} if configuration else {}
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
        cutoffs = {
            target: (
                _selection_cutoff(target, zone, issuance_cutoff)
                if issuance_cutoff is not None
                else None
            )
            for target in (
                start_date + timedelta(days=offset)
                for offset in range((end_date - start_date).days + 1)
            )
        }
        issuances = (
            select_forecast_issuances(plant, cutoffs, native_only=True)
            if comparison == "archived_forecast"
            else {}
        )
        for target, selection_cutoff in cutoffs.items():
            issuance = issuances.get(target)
            selected_configuration = issuance.configuration if issuance else configuration
            comparable = (
                selected_configuration.measurement_basis == configuration.measurement_basis
                and selected_configuration.binding.timezone == zone
            )
            if comparable and selected_configuration.pk not in measurement_cache:
                measurement_cache[selected_configuration.pk] = select_measurements(
                    selected_configuration,
                    start,
                    end,
                    now,
                    policy=measurement_policy,
                    incident_revision_ids=[],
                )[0]
            if (
                required_windows is not None
                and comparable
                and selected_configuration.pk != configuration.pk
            ):
                location = selected_configuration.binding
                required_windows[target] = daylight_window(
                    target, zone, location.latitude, location.longitude
                ).windows
            day, chosen, reference = _day(
                selected_configuration,
                target,
                measurement_cache.get(selected_configuration.pk, []),
                required_windows,
                comparison,
                now,
                selection_cutoff or (issuance.cutoff_at if issuance else None),
                issuance,
                comparable,
            )
            days.append(day)
            selected += chosen
            baseline += reference
    evaluation = evaluate_samples(
        selected, timezone=zone, limit_mw=None, required_windows=required_windows
    )
    baseline_evaluation = evaluate_samples(
        baseline, timezone=zone, limit_mw=None, required_windows=required_windows
    )
    return {
        "plant_id": plant.pk,
        "configuration_id": configuration.pk if configuration else None,
        "timezone": zone,
        "start_date": start_date,
        "end_date": end_date,
        "evaluated_at": now,
        "evaluation_kind": "retrospective_model_comparison"
        if comparison == "historical_model"
        else "archived_published_forecast",
        "comparison": comparison,
        "selection_policy": ACTUAL_COMPOSITION_POLICY
        if any(day.get("reference_provenance") for day in days)
        else HISTORY_SELECTION_POLICY
        if comparison == "historical_model"
        else ARCHIVE_SELECTION_POLICY,
        "measurement_policy": measurement_policy,
        "configuration_created_at": configuration.created_at if configuration else None,
        "measurement_basis": configuration.measurement_basis if configuration else None,
        "metric_scope": "daylight_intervals" if daylight_only else "full_day",
        "daylight_policy": DAYLIGHT_POLICY if daylight_only else None,
        "matched_days": evaluation["valid_days"],
        "missing_days": sum(
            day["reason"]
            in {
                "configuration_not_available",
                "configuration_not_comparable",
                "forecast_not_available",
                "estimated_actuals_not_available",
            }
            for day in days
        )
        if configuration is not None
        else (end_date - start_date).days + 1,
        "excluded_days": sum(
            day["reason"] in {"invalid_forecast_data", "incomplete_or_invalid_day"} for day in days
        ),
        "scored_intervals": evaluation["samples"],
        "scored_hours": sum((row.end - row.start).total_seconds() / 3600 for row in selected),
        "issuance_cutoff": issuance_cutoff,
        "requested_days": (end_date - start_date).days + 1,
        "metrics": evaluation["metrics"],
        "baseline_metrics": baseline_evaluation["metrics"],
        "days": days,
        "uncertainty": "not_estimated",
    }
