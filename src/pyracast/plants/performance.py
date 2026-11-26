from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext

from pyracast.forecasts.faults import CALCULATION_FAULTS, log_fault, stored_data
from pyracast.forecasts.outlook import Source
from pyracast.plants.day_reason import DayReason
from pyracast.plants.forecast_choice import (
    DayForecasts,
    day_total,
    past_day_forecast,
    saved_forecasts,
)
from pyracast.plants.models import Plant
from pyracast.plants.production import (
    NativeProductionDay,
    native_production_days,
    production_timezone,
)
from pyracast.plants.satellite_data import SatelliteData, satellite_data, satellite_result

# Days are compared at the forecast screen's default resolution, so they compare its figures.
RESOLUTION_MINUTES = 60


@dataclass(frozen=True)
class Figure:
    energy_mwh: float | None
    reason: DayReason | None = None
    forecast_id: int | None = None
    calibration_id: int | None = None


def production_comparison(
    plant: Plant,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    data_source: Source = "forecast",
) -> dict[str, Any]:
    """Each past day's production beside the figure the forecast screen shows for that day and
    source in automatic mode."""
    zone = production_timezone(plant)
    now = timezone.now()
    first, last = _validated_range(start_date, end_date, now.astimezone(ZoneInfo(zone)).date())
    with stored_data("plant", plant.pk, first, "production"):
        production = native_production_days(plant, first, last, now).days
    figures = (
        _forecast_figures(plant, first, last, production, now)
        if data_source == "forecast"
        else _satellite_figures(plant, first, last, production, now)
    )
    days = [_day(row, figures[row.date]) for row in production]
    return {
        "plant_id": plant.pk,
        "configuration_id": plant.current_configuration_id,
        "timezone": zone,
        "start_date": first,
        "end_date": last,
        "evaluated_at": now,
        "data_source": data_source,
        "requested_days": len(days),
        **_accuracy([day for day in days if day["status"] == "matched"]),
        "days": days,
    }


def _validated_range(start: date | None, end: date | None, today: date) -> tuple[date, date]:
    if (start is None) != (end is None):
        raise ValidationError(gettext("Enter both the start and the end date."))
    if start is None or end is None:
        end = today - timedelta(days=1)
        start = end - timedelta(days=29)
    if not 1 <= (end - start).days + 1 <= 90 or start < today - timedelta(days=366) or end >= today:
        raise ValidationError(
            gettext("Choose a date range of 1 to 90 days, before today and within the past year.")
        )
    return start, end


def _forecast_figures(
    plant: Plant,
    first: date,
    last: date,
    production: tuple[NativeProductionDay, ...],
    now: datetime,
) -> dict[date, Figure]:
    saved = saved_forecasts(plant, first, last, RESOLUTION_MINUTES, now)
    return {row.date: _forecast_figure(saved[row.date], row.total_mwh) for row in production}


def _forecast_figure(forecasts: DayForecasts, production_mwh: float | None) -> Figure:
    selection, chosen = past_day_forecast(forecasts, production_mwh)
    if chosen is None or selection.rule != "closest_to_production":
        return Figure(
            None,
            DayReason.INVALID_FORECAST_DATA
            if forecasts.unreadable
            else DayReason.FORECAST_NOT_AVAILABLE,
        )
    total = next(
        option.total_mwh
        for option in forecasts.options
        if option.forecast_id == selection.forecast_id
    )
    return Figure(total, forecast_id=chosen.snapshot_id, calibration_id=chosen.calibration_id)


def _satellite_figures(
    plant: Plant,
    first: date,
    last: date,
    production: tuple[NativeProductionDay, ...],
    now: datetime,
) -> dict[date, Figure]:
    satellite = satellite_data(plant, first, last, now)
    return {row.date: _satellite_figure(plant, row.date, satellite) for row in production}


def _satellite_figure(plant: Plant, day: date, satellite: SatelliteData) -> Figure:
    try:
        chosen = satellite.day(day)
        result = satellite_result(chosen, day, RESOLUTION_MINUTES)
        total = day_total(result) if result is not None else None
    except CALCULATION_FAULTS as error:
        log_fault("plant", plant.pk, day, "performance", error)
        return Figure(None, DayReason.INVALID_FORECAST_DATA)
    if result is None:
        return Figure(None, DayReason.ESTIMATED_ACTUALS_NOT_AVAILABLE)
    calibration = chosen.calibration.pk if chosen.calibration is not None else None
    if total is None:
        return Figure(None, DayReason.INVALID_FORECAST_DATA, calibration_id=calibration)
    return Figure(total, calibration_id=calibration)


def _day(production: NativeProductionDay, figure: Figure) -> dict[str, Any]:
    measured, predicted = production.total_mwh, figure.energy_mwh
    error = predicted - measured if measured is not None and predicted is not None else None
    return {
        "date": production.date,
        "status": "missing" if error is None else "matched",
        "reason": DayReason.INCOMPLETE_OR_INVALID_DAY if measured is None else figure.reason,
        "forecast_id": figure.forecast_id,
        "calibration_id": figure.calibration_id,
        "measured_energy_mwh": measured,
        "predicted_energy_mwh": predicted,
        "error_mwh": error,
        "provisional_production": any(
            slot.effective is not None and slot.effective["provisional"]
            for slot in production.slots
        ),
    }


def _accuracy(matched: list[dict[str, Any]]) -> dict[str, Any]:
    errors = [day["error_mwh"] for day in matched]
    measured = sum(day["measured_energy_mwh"] for day in matched)
    return {
        "matched_days": len(matched),
        "relative_error": sum(abs(error) for error in errors) / measured if measured else None,
        "daily_mae_mwh": sum(abs(error) for error in errors) / len(errors) if errors else None,
        "daily_bias_mwh": sum(errors) / len(errors) if errors else None,
    }
