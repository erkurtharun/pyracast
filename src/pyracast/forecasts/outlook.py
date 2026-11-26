from datetime import date, datetime
from enum import StrEnum
from typing import Literal, TypeIs
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError

from pyracast.errors import UNKNOWN_FORECAST, DomainError, ErrorCode
from pyracast.forecasts.history import (
    SavedResults,
    later_region_fetches,
    regional_composed_forecast,
    regional_history,
    saved_region_forecast,
    saved_region_results,
)
from pyracast.forecasts.intraday import ActualsMode
from pyracast.forecasts.selection import (
    Day,
    ForecastOption,
    ForecastSelection,
    automatic_selection,
    day_of,
    day_options,
    manual_selection,
    offers_later_fetches,
)
from pyracast.forecasts.sources import LaterFetches
from pyracast.regions.models import Region

type Source = Literal["forecast", "satellite"]

SOURCES: tuple[Source, ...] = ("forecast", "satellite")


class Policy(StrEnum):
    DAY_AHEAD = "day_ahead"
    AUTOMATIC = "automatic"
    LIVE = "live"
    LIVE_WITH_ACTUALS = "live_with_actuals"
    LIVE_ACTUALS = "live_actuals"
    ARCHIVED_ACTUALS = "archived_actuals"


LIVE_MODES: dict[Policy, ActualsMode] = {
    Policy.LIVE: "none",
    Policy.LIVE_WITH_ACTUALS: "elapsed",
    Policy.LIVE_ACTUALS: "only",
}

# The policy of an automatic plant read, and the local days, counted from today, each answers.
PLANT_POLICIES: dict[tuple[Source, Day], Policy] = {
    ("forecast", "past"): Policy.DAY_AHEAD,
    ("forecast", "today"): Policy.AUTOMATIC,
    ("forecast", "future"): Policy.AUTOMATIC,
    ("satellite", "past"): Policy.ARCHIVED_ACTUALS,
    ("satellite", "today"): Policy.LIVE_ACTUALS,
    ("satellite", "future"): Policy.ARCHIVED_ACTUALS,
}
PLANT_DAYS: dict[Policy, range] = {
    Policy.DAY_AHEAD: range(-366, 0),
    Policy.AUTOMATIC: range(15),
    Policy.LIVE_ACTUALS: range(1),
    Policy.ARCHIVED_ACTUALS: range(-366, 0),
}
REGION_DAYS = range(-366, 15)
REGION_POLICIES: dict[tuple[Source, Day], Policy | ErrorCode] = {
    ("forecast", "past"): Policy.DAY_AHEAD,
    ("forecast", "today"): Policy.LIVE_WITH_ACTUALS,
    ("forecast", "future"): Policy.LIVE,
    ("satellite", "past"): Policy.ARCHIVED_ACTUALS,
    ("satellite", "today"): Policy.LIVE_ACTUALS,
    ("satellite", "future"): ErrorCode.COMPLETED_DAY_REQUIRED,
}


def is_source(value: str) -> TypeIs[Source]:
    return value in SOURCES


def plant_policy(source: Source, target_date: date, today: date) -> Policy:
    return PLANT_POLICIES[source, day_of(target_date, today)]


def plant_days_include(policy: Policy, target_date: date, today: date) -> bool:
    return (target_date - today).days in PLANT_DAYS[policy]


def region_policy(source: Source, target_date: date, today: date) -> Policy:
    if (target_date - today).days not in REGION_DAYS:
        raise DomainError(ErrorCode.DATE_OUT_OF_RANGE)
    policy = REGION_POLICIES[source, day_of(target_date, today)]
    if isinstance(policy, ErrorCode):
        raise DomainError(policy)
    return policy


def chosen_forecast(options: tuple[ForecastOption, ...], forecast_id: int) -> ForecastSelection:
    selection = manual_selection(options, forecast_id)
    if selection is None:
        raise ValidationError({"forecast_id": [UNKNOWN_FORECAST]})
    return selection


def regional_outlook(
    region: Region, target_date: date, source: Source, forecast_id: int | None, now: datetime
) -> dict[str, object]:
    today = now.astimezone(ZoneInfo(region.timezone)).date()
    policy = region_policy(source, target_date, today)
    if source == "satellite":
        answer = (
            regional_history(region, target_date, now)
            if policy is Policy.ARCHIVED_ACTUALS
            else _live(region, target_date, now, policy)
        )
        return answer | {"selection": None}
    return _region_forecast(
        region, target_date, day_of(target_date, today), policy, forecast_id, now
    )


def _region_forecast(
    region: Region,
    target_date: date,
    day: Day,
    policy: Policy,
    forecast_id: int | None,
    now: datetime,
) -> dict[str, object]:
    saved = saved_region_results(region, target_date, now)
    later = (
        later_region_fetches(region, target_date, now)
        if offers_later_fetches(day)
        else LaterFetches()
    )
    options = day_options(saved.options, later.options)
    if forecast_id is not None:
        selection = chosen_forecast(options, forecast_id)
        known_at = later.known_at.get(forecast_id)
        if known_at is None:
            return _saved(region, target_date, saved, selection)
        answer = regional_composed_forecast(
            region, target_date, known_at, now, region.approved_binding
        )
        return answer | {"selection": selection}
    selection = automatic_selection(day, options, None)
    if policy is Policy.DAY_AHEAD:
        return _saved(region, target_date, saved, selection)
    return _live(region, target_date, now, policy) | {"selection": selection}


def _saved(
    region: Region, target_date: date, saved: SavedResults, selection: ForecastSelection
) -> dict[str, object]:
    result_id = saved.result_ids.get(selection.forecast_id) if selection.forecast_id else None
    return saved_region_forecast(region, target_date, result_id) | {"selection": selection}


def _live(region: Region, target_date: date, now: datetime, policy: Policy) -> dict[str, object]:
    return regional_composed_forecast(
        region, target_date, now, now, region.approved_binding, actuals=LIVE_MODES[policy]
    )
