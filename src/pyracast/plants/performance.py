from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext

from pyracast.forecasts.actuals import (
    ACTUAL_COMPOSITION_POLICY,
    ArchivedActualSource,
    actual_candidates,
)
from pyracast.forecasts.faults import CALCULATION_FAULTS, log_fault
from pyracast.forecasts.history import HISTORY_SELECTION_POLICY
from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecast_archive import (
    ARCHIVE_SELECTION_POLICY,
    issuance_power_intervals,
    select_forecast_issuances,
)
from pyracast.plants.forecasting import (
    CalibrationTimeline,
    calibration_timeline,
    fitted_on,
    power_intervals,
    power_limit,
)
from pyracast.plants.measurement_policy import (
    MEASUREMENT_POLICY,
    SETTLEMENT_ONLY_MEASUREMENT_POLICY,
)
from pyracast.plants.measurements import Measurement, select_plant_measurements
from pyracast.plants.models import CalibrationRun, ForecastIssuance, Plant, PlantConfiguration
from pyracast.plants.numerics import (
    CalibrationSample,
    PowerInterval,
    RequiredWindows,
    evaluate_samples,
)
from pyracast.plants.transfer import transfer_model
from pyracast.solar import DAYLIGHT_POLICY, daylight_window


class DayReason(models.TextChoices):
    CONFIGURATION_NOT_COMPARABLE = "configuration_not_comparable"
    FORECAST_NOT_AVAILABLE = "forecast_not_available"
    ESTIMATED_ACTUALS_NOT_AVAILABLE = "estimated_actuals_not_available"
    INVALID_FORECAST_DATA = "invalid_forecast_data"
    INCOMPLETE_OR_INVALID_DAY = "incomplete_or_invalid_day"


class SelectionPolicy(models.TextChoices):
    ARCHIVED_ISSUANCE = ARCHIVE_SELECTION_POLICY
    ESTIMATED_ACTUALS = HISTORY_SELECTION_POLICY
    COMPOSED_ESTIMATED_ACTUALS = ACTUAL_COMPOSITION_POLICY


MISSING_REASONS = {
    DayReason.CONFIGURATION_NOT_COMPARABLE,
    DayReason.FORECAST_NOT_AVAILABLE,
    DayReason.ESTIMATED_ACTUALS_NOT_AVAILABLE,
}
EXCLUDED_REASONS = {DayReason.INVALID_FORECAST_DATA, DayReason.INCOMPLETE_OR_INVALID_DAY}


@dataclass(frozen=True)
class DaySource:
    """What one day is scored with: an archived issuance, or the model run on the day's
    estimated actuals with the calibration fitted for that period."""

    issuance: ForecastIssuance | None
    actual: ArchivedActualSource | None
    calibration: CalibrationRun | None


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


def _unscored(
    configuration: PlantConfiguration,
    day: date,
    issuance: ForecastIssuance | None,
    selection_cutoff: datetime | None,
) -> dict[str, Any]:
    return {
        "date": day,
        "status": "missing",
        "reason": None,
        "selection_policy": ARCHIVE_SELECTION_POLICY if issuance is not None else None,
        "snapshot_id": None,
        "issuance_id": issuance.pk if issuance is not None else None,
        "configuration_id": configuration.pk,
        "model_version": issuance.model_version if issuance is not None else None,
        "collected_at": None,
        "available_at": None,
        "calibration_id": None,
        "calibration_in_sample": None,
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


def _readings(
    observations: list[Measurement],
    day: date,
    zone: str,
    required_windows: RequiredWindows | None,
) -> list[Measurement]:
    start, end = local_day_bounds(day, zone)
    return [
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


def _paired_samples(
    configuration: PlantConfiguration,
    source: DaySource,
    readings: list[Measurement],
    factor: float,
    model_version: str,
) -> tuple[tuple[CalibrationSample, ...], tuple[CalibrationSample, ...]]:
    if source.issuance is not None:
        return (
            _samples(readings, issuance_power_intervals(source.issuance), 1.0, None),
            _samples(readings, issuance_power_intervals(source.issuance, baseline=True), 1.0, None),
        )
    actual = cast(ArchivedActualSource, source.actual)
    intervals = power_intervals(
        actual.snapshot,
        configuration,
        model_version=model_version,
        source_intervals=actual.composition.intervals if actual.composition is not None else None,
    )
    cap = power_limit(configuration)
    return _samples(readings, intervals, factor, cap), _samples(readings, intervals, 1.0, cap)


def _apply_source(
    result: dict[str, Any], configuration: PlantConfiguration, day: date, source: DaySource
) -> float:
    issuance, actual, calibration = source.issuance, source.actual, source.calibration
    snapshot = (
        issuance.snapshot if issuance is not None else cast(ArchivedActualSource, actual).snapshot
    )
    factor = (
        issuance.factor
        if issuance is not None
        else cast(float, calibration.factor)
        if calibration is not None
        else 1.0
    )
    start, _ = local_day_bounds(day, configuration.binding.timezone)
    result.update(
        snapshot_id=snapshot.pk,
        collected_at=snapshot.collected_at,
        available_at=issuance.published_at if issuance is not None else snapshot.available_at,
        calibration_id=calibration.pk if calibration else None,
        # An issuance's calibration was fitted before its publication, so before the day.
        calibration_in_sample=None
        if calibration is None
        else issuance is None and fitted_on(calibration, day),
        factor=factor,
        source_kind=snapshot.source_kind,
        historical_configuration_assumed=issuance is None and configuration.created_at >= start,
    )
    if actual is not None:
        result["model_version"] = transfer_model(configuration, calibration)
        result["selection_policy"] = (
            ACTUAL_COMPOSITION_POLICY
            if actual.composition is not None
            else HISTORY_SELECTION_POLICY
        )
    if actual is not None and actual.composition is not None:
        result["source_snapshot_ids"] = list(actual.composition.snapshot_ids)
        result["reference_provenance"] = {
            "selection_policy": ACTUAL_COMPOSITION_POLICY,
            "source_snapshot_ids": list(actual.composition.snapshot_ids),
            "source_segments": actual.composition.segments,
            "oldest_collected_at": min(row.collected_at for row in actual.snapshots).isoformat(),
        }
    return factor


def _matched(
    result: dict[str, Any],
    readings: list[Measurement],
    errors: list[dict[str, Any]],
    reference: list[dict[str, Any]],
) -> None:
    result.update(
        status="matched",
        measured_energy_mwh=errors[0]["measured_energy_mwh"],
        predicted_energy_mwh=errors[0]["predicted_energy_mwh"],
        baseline_energy_mwh=reference[0]["predicted_energy_mwh"],
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


def _day(
    configuration: PlantConfiguration,
    day: date,
    observations: list[Measurement],
    required_windows: RequiredWindows | None,
    selection_cutoff: datetime | None,
    issuance: ForecastIssuance | None,
    comparable: bool,
    actuals: Callable[[date], DaySource] | None,
) -> tuple[dict[str, Any], tuple[CalibrationSample, ...], tuple[CalibrationSample, ...]]:
    result = _unscored(configuration, day, issuance, selection_cutoff)
    if not comparable:
        result["reason"] = DayReason.CONFIGURATION_NOT_COMPARABLE
        return result, (), ()
    zone = configuration.binding.timezone
    try:
        source = (
            DaySource(issuance, None, issuance.calibration if issuance else None)
            if actuals is None
            else actuals(day)
        )
        if source.issuance is None and source.actual is None:
            result["reason"] = (
                DayReason.FORECAST_NOT_AVAILABLE
                if actuals is None
                else DayReason.ESTIMATED_ACTUALS_NOT_AVAILABLE
            )
            return result, (), ()
        factor = _apply_source(result, configuration, day, source)
        readings = _readings(observations, day, zone, required_windows)
        selected, baseline = _paired_samples(
            configuration, source, readings, factor, result["model_version"]
        )
        evaluation = evaluate_samples(
            selected, timezone=zone, limit_mw=None, required_windows=required_windows
        )
        reference = evaluate_samples(
            baseline, timezone=zone, limit_mw=None, required_windows=required_windows
        )
    except CALCULATION_FAULTS as error:
        log_fault("plant", configuration.plant_id, day, "performance", error)
        result["reason"] = DayReason.INVALID_FORECAST_DATA
        return result, (), ()
    if not evaluation["valid_days"] or not reference["valid_days"]:
        result["reason"] = DayReason.INCOMPLETE_OR_INVALID_DAY
        return result, (), ()
    _matched(
        result,
        readings,
        cast(list[dict[str, Any]], evaluation["day_errors"]),
        cast(list[dict[str, Any]], reference["day_errors"]),
    )
    return result, selected, baseline


def _validated_range(
    comparison: str,
    issuance_cutoff: time | None,
    start_date: date | None,
    end_date: date | None,
    today: date,
) -> tuple[date, date]:
    if comparison not in ("archived_forecast", "historical_model"):
        raise ValidationError(gettext("Unknown comparison mode."))
    if issuance_cutoff is not None:
        if issuance_cutoff.tzinfo is not None:
            raise ValidationError(gettext("Use a local issuance cutoff without a UTC offset."))
        if comparison != "archived_forecast":
            raise ValidationError(
                gettext("An issuance cutoff requires archived_forecast comparison.")
            )
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
    return start_date, end_date


def _historical_actuals(
    configuration: PlantConfiguration, first: date, last: date, now: datetime
) -> Callable[[date], DaySource]:
    candidates = actual_candidates(configuration.binding, first, last, now)
    timeline: CalibrationTimeline | None = None

    def select(day: date) -> DaySource:
        nonlocal timeline
        actual = candidates.select(day)
        if actual is None:
            return DaySource(None, None, None)
        if timeline is None:
            timeline = calibration_timeline(configuration, now)
        return DaySource(None, actual, timeline.for_day(day))

    return select


def _accuracy(days: list[dict[str, Any]], comparison: str) -> dict[str, Any]:
    """Relative error over every matched day; skill against the uncalibrated reference only
    on days the calibration did not train on."""
    matched = [day for day in days if day["status"] == "matched"]
    measured = sum(day["measured_energy_mwh"] for day in matched)
    scored = [
        day
        for day in matched
        if comparison == "archived_forecast" or day["calibration_in_sample"] is False
    ]
    reference = sum(abs(day["baseline_energy_mwh"] - day["measured_energy_mwh"]) for day in scored)
    return {
        "relative_error": sum(abs(day["error_mwh"]) for day in matched) / measured
        if measured
        else None,
        "skill_score": 1 - sum(abs(day["error_mwh"]) for day in scored) / reference
        if reference
        else None,
        "skill_days": len(scored),
    }


def forecast_performance(
    plant: Plant,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    daylight_only: bool = False,
    comparison: str = "archived_forecast",
    issuance_cutoff: time | None = None,
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
    start_date, end_date = _validated_range(
        comparison, issuance_cutoff, start_date, end_date, now.astimezone(ZoneInfo(zone)).date()
    )
    targets = [
        start_date + timedelta(days=offset) for offset in range((end_date - start_date).days + 1)
    ]
    start, _ = local_day_bounds(start_date, zone)
    _, end = local_day_bounds(end_date, zone)
    historical = comparison == "historical_model"
    measurement_policy = MEASUREMENT_POLICY if historical else SETTLEMENT_ONLY_MEASUREMENT_POLICY
    required_windows = None
    if daylight_only:
        location = configuration.binding if configuration else plant.region
        required_windows = {
            target: daylight_window(target, zone, location.latitude, location.longitude).windows
            for target in targets
        }
    days: list[dict[str, Any]] = []
    selected: tuple[CalibrationSample, ...] = ()
    baseline: tuple[CalibrationSample, ...] = ()
    if configuration is not None:
        cutoffs = {
            target: _selection_cutoff(target, zone, issuance_cutoff)
            if issuance_cutoff is not None
            else None
            for target in targets
        }
        issuances = (
            {} if historical else select_forecast_issuances(plant, cutoffs, native_only=True)
        )
        actuals = (
            _historical_actuals(configuration, start_date, end_date, now) if historical else None
        )
        comparable_configurations = {configuration.pk: configuration} | {
            row.configuration.pk: row.configuration
            for row in issuances.values()
            if row.configuration.measurement_basis == configuration.measurement_basis
            and row.configuration.binding.timezone == zone
        }
        measurement_cache = select_plant_measurements(
            list(comparable_configurations.values()),
            start,
            end,
            now,
            policy=measurement_policy,
            incident_revision_ids=None if historical else [],
        )
        for target, selection_cutoff in cutoffs.items():
            issuance = issuances.get(target)
            selected_configuration = issuance.configuration if issuance else configuration
            comparable = selected_configuration.pk in comparable_configurations
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
                selection_cutoff or (issuance.cutoff_at if issuance else None),
                issuance,
                comparable,
                actuals,
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
        "comparison": comparison,
        "selection_policy": ACTUAL_COMPOSITION_POLICY
        if any(day.get("reference_provenance") for day in days)
        else HISTORY_SELECTION_POLICY
        if historical
        else ARCHIVE_SELECTION_POLICY,
        "measurement_policy": measurement_policy,
        "configuration_created_at": configuration.created_at if configuration else None,
        "measurement_basis": configuration.measurement_basis if configuration else None,
        "metric_scope": "daylight_intervals" if daylight_only else "full_day",
        "daylight_policy": DAYLIGHT_POLICY if daylight_only else None,
        "matched_days": evaluation["valid_days"],
        "missing_days": sum(day["reason"] in MISSING_REASONS for day in days)
        if configuration is not None
        else len(targets),
        "excluded_days": sum(day["reason"] in EXCLUDED_REASONS for day in days),
        "scored_intervals": evaluation["samples"],
        "scored_hours": sum((row.end - row.start).total_seconds() / 3600 for row in selected),
        "issuance_cutoff": issuance_cutoff,
        "requested_days": len(targets),
        "metrics": evaluation["metrics"],
        "baseline_metrics": baseline_evaluation["metrics"],
        **_accuracy(days, comparison),
        "days": days,
        "uncertainty": "not_estimated",
    }
