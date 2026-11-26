from datetime import UTC, datetime, timedelta

from pyracast.forecasts.selection import (
    ForecastOption,
    automatic_selection,
    day_options,
    is_day_ahead,
    manual_selection,
    offers_later_fetches,
)

SUNRISE = datetime(2026, 11, 7, 4, tzinfo=UTC)


def _option(
    forecast_id: int, hours: int, total: float | None, *, day_ahead: bool = True
) -> ForecastOption:
    return ForecastOption(forecast_id, SUNRISE + timedelta(hours=hours), day_ahead, total)


def test_the_owners_example_chooses_the_total_nearest_to_production() -> None:
    options = day_options([_option(1, 6, 520), _option(2, 13, 506)], [])

    below = automatic_selection("past", options, 500)
    above = automatic_selection("past", options, 530)

    assert (below.mode, below.rule, below.forecast_id) == ("auto", "closest_to_production", 2)
    assert (above.mode, above.rule, above.forecast_id) == ("auto", "closest_to_production", 1)
    assert [option.forecast_id for option in below.options] == [2, 1]


def test_equal_distances_choose_the_later_fetch() -> None:
    options = day_options([_option(1, 13, 520), _option(2, 6, 480)], [])

    assert automatic_selection("past", options, 500).forecast_id == 1


def test_without_production_the_latest_day_ahead_forecast_is_chosen() -> None:
    options = day_options(
        [_option(1, 6, 520), _option(2, 13, 506), _option(3, 20, 300, day_ahead=False)], []
    )

    selection = automatic_selection("past", options, None)

    assert (selection.rule, selection.forecast_id) == ("latest", 2)


def test_forecasts_without_a_day_total_are_not_compared_with_production() -> None:
    options = day_options([_option(1, 6, 500), _option(2, 13, None)], [])

    assert automatic_selection("past", options, 500).forecast_id == 1
    assert automatic_selection("past", day_options([_option(2, 13, None)], []), 500).rule == (
        "latest"
    )


def test_without_a_day_ahead_forecast_the_latest_saved_one_is_chosen() -> None:
    options = day_options(
        [_option(1, 6, 500, day_ahead=False), _option(2, -3, 400, day_ahead=False)], []
    )

    selection = automatic_selection("past", options, 500)

    assert (selection.rule, selection.forecast_id) == ("latest", 1)
    assert automatic_selection("past", (), 500).forecast_id is None


def test_today_and_future_days_show_the_newest_fetch() -> None:
    options = day_options([_option(1, 6, 500)], [_option(2, 30, None)])

    for day in ("today", "future"):
        selection = automatic_selection(day, options, 400)
        assert (selection.rule, selection.forecast_id) == ("latest", 2)


def test_only_today_offers_fetches_made_after_sunrise() -> None:
    assert [offers_later_fetches(day) for day in ("past", "today", "future")] == [
        False,
        True,
        False,
    ]


def test_a_chosen_forecast_must_be_an_option() -> None:
    options = day_options([_option(1, 6, 500), _option(2, 13, 400)], [])

    chosen = manual_selection(options, 1)

    assert chosen is not None
    assert (chosen.mode, chosen.rule, chosen.forecast_id) == ("manual", None, 1)
    assert manual_selection(options, 3) is None


def test_the_approval_fetch_and_fetches_before_the_previous_sunrise_are_not_day_ahead() -> None:
    assert is_day_ahead(SUNRISE, SUNRISE, by_approval=False)
    assert not is_day_ahead(SUNRISE - timedelta(seconds=1), SUNRISE, by_approval=False)
    assert not is_day_ahead(SUNRISE + timedelta(hours=6), SUNRISE, by_approval=True)
