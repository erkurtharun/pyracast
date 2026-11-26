from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from pyracast.connections.models import SiteBinding
from pyracast.errors import DomainError, ErrorCode
from pyracast.forecasts.faults import DamagedStoredDataError, log_fault
from pyracast.forecasts.outlook import Policy, chosen_forecast
from pyracast.forecasts.selection import (
    Day,
    ForecastOption,
    ForecastSelection,
    automatic_selection,
    day_options,
    is_day_ahead,
    live_selection,
    offers_later_fetches,
)
from pyracast.forecasts.sources import LaterFetches, later_fetches, supplying_fetches
from pyracast.irradiance import local_day_bounds
from pyracast.plants.automatic_forecast import automatic_forecast
from pyracast.plants.current_forecast import current_forecast
from pyracast.plants.forecast_archive import forecast_cutoff, saved_issuances
from pyracast.plants.forecasting import decoded_issuance, issuance_outlook, missing_forecast
from pyracast.plants.models import ForecastIssuance, Plant, PlantConfiguration
from pyracast.plants.production import production_total


@dataclass(frozen=True)
class DayForecasts:
    issuances: dict[int, ForecastIssuance]
    unreadable: dict[int, DamagedStoredDataError]
    later: LaterFetches
    options: tuple[ForecastOption, ...]


def forecast_outlook(
    plant: Plant,
    target_date: date,
    resolution_minutes: int,
    *,
    day: Day,
    policy: Policy,
    forecast_id: int | None,
    now: datetime,
) -> dict[str, object]:
    forecasts = _day_forecasts(plant, target_date, resolution_minutes, day, now)
    if forecast_id is not None:
        return _chosen_outlook(plant, target_date, resolution_minutes, forecasts, forecast_id, now)
    if policy is Policy.DAY_AHEAD:
        selection, chosen = past_day_forecast(forecasts, production_total(plant, target_date, now))
        # A day whose every saved forecast is unreadable is refused, never shown without one.
        if chosen is None and forecasts.unreadable:
            damage = next(iter(forecasts.unreadable.values()))
            raise DomainError(ErrorCode.CALCULATION_OUT_OF_RANGE) from damage
        answer = (
            issuance_outlook(plant, chosen, target_date, resolution_minutes, now)
            if chosen is not None
            else missing_forecast(plant, target_date, now)
        )
        return answer | {"selection": selection}
    answer = automatic_forecast(plant, target_date, resolution_minutes, now)
    suppliers = supplying_fetches(answer["result"])
    return answer | {"selection": live_selection(forecasts.options, suppliers)}


def past_day_forecast(
    forecasts: DayForecasts, production_mwh: float | None
) -> tuple[ForecastSelection, ForecastIssuance | None]:
    """The saved forecast a past day shows automatically, and the selection that names it."""
    selection = automatic_selection(forecasts.options, production_mwh)
    chosen = forecasts.issuances.get(selection.forecast_id) if selection.forecast_id else None
    return selection, chosen


def saved_forecasts(
    plant: Plant, first: date, last: date, resolution_minutes: int, now: datetime
) -> dict[date, DayForecasts]:
    """The saved forecasts of each day from `first` to `last`, read once for the range."""
    saved = saved_issuances(plant, first, last, now)
    days = (first + timedelta(days=offset) for offset in range((last - first).days + 1))
    return {
        day: _forecasts(plant, day, resolution_minutes, saved.get(day, []), LaterFetches())
        for day in days
    }


def day_total(result: dict[str, Any]) -> float | None:
    """The day total the answer shows: the daylight energy of a complete day, in MWh."""
    daylight: dict[str, Any] | None = result.get("daylight")
    summary = daylight if daylight is not None else result
    shown = summary.get("complete") is True or (
        daylight is not None and daylight.get("status") == "polar_night"
    )
    energy = summary.get("energy_mwh")
    return float(energy) if shown and energy is not None else None


def _chosen_outlook(
    plant: Plant,
    target_date: date,
    resolution_minutes: int,
    forecasts: DayForecasts,
    forecast_id: int,
    now: datetime,
) -> dict[str, object]:
    damage = forecasts.unreadable.get(forecast_id)
    if damage is not None:
        raise DomainError(ErrorCode.CALCULATION_OUT_OF_RANGE) from damage
    selection = chosen_forecast(forecasts.options, forecast_id)
    later = forecasts.later.fetches.get(forecast_id)
    answer = (
        current_forecast(plant, target_date, resolution_minutes, now, chosen=later)
        if later is not None
        else issuance_outlook(
            plant, forecasts.issuances[forecast_id], target_date, resolution_minutes, now
        )
    )
    return answer | {"selection": selection}


def _day_forecasts(
    plant: Plant, target_date: date, resolution_minutes: int, day: Day, now: datetime
) -> DayForecasts:
    saved = saved_issuances(plant, target_date, target_date, now).get(target_date, [])
    later = _later_fetches(plant, target_date, now) if offers_later_fetches(day) else LaterFetches()
    return _forecasts(plant, target_date, resolution_minutes, saved, later)


def _forecasts(
    plant: Plant,
    target_date: date,
    resolution_minutes: int,
    saved: list[tuple[ForecastIssuance, bool]],
    later: LaterFetches,
) -> DayForecasts:
    totals, unreadable = _day_totals(plant, target_date, resolution_minutes, saved)
    readable = [pair for pair in saved if pair[0].snapshot_id in totals]
    bindings = {
        issuance.configuration.binding_id: issuance.configuration.binding
        for issuance, _ in readable
    }
    previous_day = target_date - timedelta(days=1)
    previous_sunrise = {
        binding_id: forecast_cutoff(previous_day, binding)
        for binding_id, binding in bindings.items()
    }
    options = (
        ForecastOption(
            issuance.snapshot_id,
            issuance.snapshot.collected_at,
            is_day_ahead(
                issuance.snapshot.collected_at,
                previous_sunrise[issuance.configuration.binding_id],
                by_approval=by_approval,
            ),
            totals[issuance.snapshot_id],
        )
        for issuance, by_approval in readable
    )
    return DayForecasts(
        {issuance.snapshot_id: issuance for issuance, _ in readable},
        unreadable,
        later,
        day_options(options, later.options),
    )


def _day_totals(
    plant: Plant,
    target_date: date,
    resolution_minutes: int,
    saved: list[tuple[ForecastIssuance, bool]],
) -> tuple[dict[int, float | None], dict[int, DamagedStoredDataError]]:
    """By forecast: the day total of each saved forecast, and the damage of each one that
    cannot be decoded."""
    totals: dict[int, float | None] = {}
    unreadable: dict[int, DamagedStoredDataError] = {}
    for issuance, _ in saved:
        try:
            result, _ = decoded_issuance(issuance, str(resolution_minutes))
        except DamagedStoredDataError as damage:
            log_fault("plant", plant.pk, target_date, "candidates", damage)
            unreadable[issuance.snapshot_id] = damage
        else:
            totals[issuance.snapshot_id] = day_total(result)
    return totals, unreadable


def _later_fetches(plant: Plant, target_date: date, now: datetime) -> LaterFetches:
    binding = (
        SiteBinding.objects.filter(
            pk__in=PlantConfiguration.objects.filter(pk=plant.current_configuration_id).values(
                "binding_id"
            )
        ).first()
        if plant.current_configuration_id is not None
        else None
    )
    if binding is None:
        return LaterFetches()
    start, end = local_day_bounds(target_date, binding.timezone)
    return later_fetches(binding.pk, start, end, forecast_cutoff(target_date, binding), now)
