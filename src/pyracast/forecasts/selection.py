from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, get_args

type Day = Literal["past", "today", "future"]
type SelectionMode = Literal["auto", "manual"]
type SelectionRule = Literal["closest_to_production", "latest"]
SELECTION_MODES: tuple[str, ...] = get_args(SelectionMode.__value__)
SELECTION_RULES: tuple[str, ...] = get_args(SelectionRule.__value__)


@dataclass(frozen=True)
class ForecastOption:
    forecast_id: int
    fetched_at: datetime
    day_ahead: bool = False
    total_mwh: float | None = None


@dataclass(frozen=True)
class ForecastSelection:
    mode: SelectionMode
    rule: SelectionRule | None
    forecast_id: int | None
    options: tuple[ForecastOption, ...]


def day_of(target_date: date, today: date) -> Day:
    if target_date < today:
        return "past"
    return "today" if target_date == today else "future"


def is_day_ahead(fetched_at: datetime, previous_sunrise: datetime, *, by_approval: bool) -> bool:
    # Claim runs a forecast job only at an update time of its schedule, except the approval job.
    return not by_approval and fetched_at >= previous_sunrise


def offers_later_fetches(day: Day) -> bool:
    """Only today offers fetches made after the day's sunrise; for a past day they stay live
    data and never become a forecast of that day."""
    return day == "today"


def day_options(
    saved: Iterable[ForecastOption], later: Iterable[ForecastOption]
) -> tuple[ForecastOption, ...]:
    return tuple(
        sorted(
            (*saved, *later),
            key=lambda option: (option.fetched_at, option.forecast_id),
            reverse=True,
        )
    )


def automatic_selection(
    day: Day, options: tuple[ForecastOption, ...], production_mwh: float | None
) -> ForecastSelection:
    """`options` come newest first, as `day_options` returns them."""
    if day != "past":
        return _auto(options, "latest", _newest(options))
    eligible = tuple(option for option in options if option.day_ahead)
    closest = _closest(eligible, production_mwh)
    if closest is not None:
        return _auto(options, "closest_to_production", closest)
    return _auto(options, "latest", _newest(eligible) or _newest(options))


def manual_selection(
    options: tuple[ForecastOption, ...], forecast_id: int
) -> ForecastSelection | None:
    if all(option.forecast_id != forecast_id for option in options):
        return None
    return ForecastSelection("manual", None, forecast_id, options)


def _closest(
    options: tuple[ForecastOption, ...], production_mwh: float | None
) -> ForecastOption | None:
    if production_mwh is None:
        return None
    distances = [
        (abs(option.total_mwh - production_mwh), option)
        for option in options
        if option.total_mwh is not None
    ]
    # `min` keeps the first of equal distances, which is the later fetch.
    return min(distances, key=lambda pair: pair[0])[1] if distances else None


def _newest(options: tuple[ForecastOption, ...]) -> ForecastOption | None:
    return options[0] if options else None


def _auto(
    options: tuple[ForecastOption, ...], rule: SelectionRule, shown: ForecastOption | None
) -> ForecastSelection:
    return ForecastSelection("auto", rule, shown.forecast_id if shown else None, options)
