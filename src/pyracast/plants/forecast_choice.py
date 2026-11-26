from dataclasses import dataclass
from datetime import date, datetime, timedelta

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.outlook import Policy, chosen_forecast
from pyracast.forecasts.selection import (
    Day,
    ForecastOption,
    automatic_selection,
    day_options,
    is_day_ahead,
    offers_later_fetches,
)
from pyracast.forecasts.sources import LaterFetches, later_fetches
from pyracast.irradiance import local_day_bounds
from pyracast.plants.automatic_forecast import automatic_forecast
from pyracast.plants.current_forecast import current_forecast
from pyracast.plants.forecast_archive import day_issuances, forecast_cutoff
from pyracast.plants.forecasting import issuance_outlook, missing_forecast
from pyracast.plants.models import ForecastIssuance, Plant, PlantConfiguration
from pyracast.plants.production import production_total


@dataclass(frozen=True)
class DayForecasts:
    issuances: dict[int, ForecastIssuance]
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
        production = production_total(plant, target_date, now)
        selection = automatic_selection(day, forecasts.options, production)
        chosen = forecasts.issuances.get(selection.forecast_id) if selection.forecast_id else None
        answer = (
            issuance_outlook(plant, chosen, target_date, resolution_minutes, now)
            if chosen is not None
            else missing_forecast(plant, target_date, now)
        )
        return answer | {"selection": selection}
    selection = automatic_selection(day, forecasts.options, None)
    answer = automatic_forecast(plant, target_date, resolution_minutes, now)
    return answer | {"selection": selection}


def _chosen_outlook(
    plant: Plant,
    target_date: date,
    resolution_minutes: int,
    forecasts: DayForecasts,
    forecast_id: int,
    now: datetime,
) -> dict[str, object]:
    selection = chosen_forecast(forecasts.options, forecast_id)
    known_at = forecasts.later.known_at.get(forecast_id)
    answer = (
        current_forecast(plant, target_date, resolution_minutes, now, known_at=known_at)
        if known_at is not None
        else issuance_outlook(
            plant, forecasts.issuances[forecast_id], target_date, resolution_minutes, now
        )
    )
    return answer | {"selection": selection}


def _day_forecasts(
    plant: Plant, target_date: date, resolution_minutes: int, day: Day, now: datetime
) -> DayForecasts:
    saved = day_issuances(plant, target_date, now)
    later = _later_fetches(plant, target_date, now) if offers_later_fetches(day) else LaterFetches()
    previous_day = target_date - timedelta(days=1)
    previous_sunrise = {
        issuance.configuration.binding_id: forecast_cutoff(
            previous_day, issuance.configuration.binding
        )
        for issuance, _ in saved
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
            _day_total(issuance.payloads.get(str(resolution_minutes))),
        )
        for issuance, by_approval in saved
    )
    return DayForecasts(
        {issuance.snapshot_id: issuance for issuance, _ in saved},
        later,
        day_options(options, later.options),
    )


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


def _day_total(payload: object) -> float | None:
    """The day total the answer shows: the daylight energy of a complete day, in MWh."""
    if not isinstance(payload, dict):
        return None
    daylight = payload.get("daylight")
    summary = daylight if isinstance(daylight, dict) else payload
    shown = summary.get("complete") is True or (
        isinstance(daylight, dict) and daylight.get("status") == "polar_night"
    )
    energy = summary.get("energy_mwh")
    if not shown or isinstance(energy, bool) or not isinstance(energy, int | float):
        return None
    return float(energy)
