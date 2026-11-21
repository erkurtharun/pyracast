from datetime import date, datetime
from enum import StrEnum
from typing import Literal, TypeIs
from zoneinfo import ZoneInfo

from pyracast.errors import DomainError, ErrorCode
from pyracast.forecasts.history import (
    regional_composed_forecast,
    regional_history,
    saved_region_forecast,
)
from pyracast.forecasts.intraday import ActualsMode
from pyracast.regions.models import Region

type Source = Literal["auto", "forecast", "estimated_actuals", "archived_forecast"]
type Day = Literal["past", "today", "future"]

SOURCES: tuple[Source, ...] = ("auto", "forecast", "estimated_actuals", "archived_forecast")


class Policy(StrEnum):
    ARCHIVE = "archive"
    ISSUANCE = "issuance"
    LIVE = "live"
    LIVE_WITH_ACTUALS = "live_with_actuals"
    AUTOMATIC = "automatic"
    LIVE_ACTUALS = "live_actuals"
    ARCHIVED_ACTUALS = "archived_actuals"
    ACTUALS_THEN_SAVED_FORECAST = "actuals_then_saved_forecast"


LIVE_MODES: dict[Policy, ActualsMode] = {
    Policy.LIVE: "none",
    Policy.LIVE_WITH_ACTUALS: "elapsed",
    Policy.LIVE_ACTUALS: "only",
}

# The policy of a plant read without and with `as_of`, and the local days, counted from today,
# each policy answers.
PLANT_POLICIES: dict[tuple[Source, Day], tuple[Policy, Policy]] = {
    ("auto", "past"): (Policy.ACTUALS_THEN_SAVED_FORECAST, Policy.ACTUALS_THEN_SAVED_FORECAST),
    ("auto", "today"): (Policy.AUTOMATIC, Policy.ISSUANCE),
    ("auto", "future"): (Policy.AUTOMATIC, Policy.ISSUANCE),
    ("forecast", "past"): (Policy.ISSUANCE, Policy.ISSUANCE),
    ("forecast", "today"): (Policy.LIVE, Policy.ISSUANCE),
    ("forecast", "future"): (Policy.LIVE, Policy.ISSUANCE),
    ("estimated_actuals", "past"): (Policy.ARCHIVED_ACTUALS, Policy.ARCHIVED_ACTUALS),
    ("estimated_actuals", "today"): (Policy.LIVE_ACTUALS, Policy.ARCHIVED_ACTUALS),
    ("estimated_actuals", "future"): (Policy.ARCHIVED_ACTUALS, Policy.ARCHIVED_ACTUALS),
    ("archived_forecast", "past"): (Policy.ARCHIVE, Policy.ARCHIVE),
    ("archived_forecast", "today"): (Policy.ARCHIVE, Policy.ARCHIVE),
    ("archived_forecast", "future"): (Policy.ARCHIVE, Policy.ARCHIVE),
}
PLANT_DAYS: dict[Policy, range] = {
    Policy.ARCHIVE: range(-366, 1),
    Policy.ISSUANCE: range(-366, 15),
    Policy.LIVE: range(15),
    Policy.AUTOMATIC: range(15),
    Policy.LIVE_ACTUALS: range(1),
    Policy.ARCHIVED_ACTUALS: range(-366, 0),
    Policy.ACTUALS_THEN_SAVED_FORECAST: range(-366, 0),
}
REGION_DAYS = range(-366, 15)
REGION_POLICIES: dict[tuple[Source, Day], Policy | ErrorCode] = {
    ("auto", "past"): Policy.ACTUALS_THEN_SAVED_FORECAST,
    ("auto", "today"): Policy.LIVE_WITH_ACTUALS,
    ("auto", "future"): Policy.LIVE,
    ("forecast", "past"): ErrorCode.DATE_OUT_OF_RANGE,
    ("forecast", "today"): Policy.LIVE,
    ("forecast", "future"): Policy.LIVE,
    ("estimated_actuals", "past"): Policy.ARCHIVED_ACTUALS,
    ("estimated_actuals", "today"): Policy.LIVE_ACTUALS,
    ("estimated_actuals", "future"): ErrorCode.COMPLETED_DAY_REQUIRED,
    ("archived_forecast", "past"): Policy.ARCHIVE,
    ("archived_forecast", "today"): Policy.ARCHIVE,
    ("archived_forecast", "future"): ErrorCode.DATE_OUT_OF_RANGE,
}


def is_source(value: str) -> TypeIs[Source]:
    return value in SOURCES


def day_of(target_date: date, today: date) -> Day:
    if target_date < today:
        return "past"
    return "today" if target_date == today else "future"


def plant_policy(source: Source, target_date: date, today: date, *, as_of: bool) -> Policy:
    return PLANT_POLICIES[source, day_of(target_date, today)][as_of]


def plant_days_include(policy: Policy, target_date: date, today: date) -> bool:
    return (target_date - today).days in PLANT_DAYS[policy]


def region_policy(source: Source, target_date: date, today: date) -> Policy:
    if (target_date - today).days not in REGION_DAYS:
        raise DomainError(ErrorCode.DATE_OUT_OF_RANGE)
    policy = REGION_POLICIES[source, day_of(target_date, today)]
    if isinstance(policy, ErrorCode):
        raise DomainError(policy)
    return policy


def regional_outlook(
    region: Region, target_date: date, source: Source, now: datetime
) -> dict[str, object]:
    policy = region_policy(source, target_date, now.astimezone(ZoneInfo(region.timezone)).date())
    match policy:
        case Policy.ARCHIVE:
            return saved_region_forecast(region, target_date, now)
        case Policy.ARCHIVED_ACTUALS:
            return regional_history(region, target_date, now)
        case Policy.ACTUALS_THEN_SAVED_FORECAST:
            return regional_history(region, target_date, now, fallback_forecast=True)
    return regional_composed_forecast(
        region, target_date, now, now, region.approved_binding, actuals=LIVE_MODES[policy]
    )
