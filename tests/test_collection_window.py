from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from pyracast.collection_window import nightly_window
from pyracast.forecasts.timing import next_forecast_slot


def test_night_window_uses_region_timezone() -> None:
    assert nightly_window(date(2026, 11, 12), "Europe/Istanbul") == (
        datetime(2026, 11, 11, 21, tzinfo=UTC),
        datetime(2026, 11, 12, 6, tzinfo=UTC),
    )


def test_night_window_starts_at_first_real_half_hour_after_clock_gap() -> None:
    assert nightly_window(date(2026, 9, 6), "America/Santiago") == (
        datetime(2026, 9, 6, 4, tzinfo=UTC),
        datetime(2026, 9, 6, 12, tzinfo=UTC),
    )


def test_night_window_uses_first_repeated_midnight() -> None:
    assert nightly_window(date(2015, 11, 1), "America/Havana") == (
        datetime(2015, 11, 1, 4, tzinfo=UTC),
        datetime(2015, 11, 1, 14, tzinfo=UTC),
    )


def test_skipped_calendar_day_has_no_night_window() -> None:
    assert nightly_window(date(2011, 12, 30), "Pacific/Apia") is None


def test_retry_does_not_start_at_deadline_when_the_entire_window_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def skipped_window(day: date, clock: str, zone: ZoneInfo) -> datetime | None:
        return datetime(2026, 1, 1, 9, tzinfo=UTC) if clock == "09:00" else None

    monkeypatch.setattr("pyracast.collection_window.local_slot", skipped_window)
    assert nightly_window(date(2026, 1, 1), "UTC") is None


def test_next_forecast_slot_skips_days_without_the_local_time() -> None:
    now = datetime(2026, 3, 28, 12, tzinfo=UTC)

    slot = next_forecast_slot(now, "Europe/Berlin", ["02:30"], now - timedelta(days=1))

    assert slot == datetime(2026, 3, 30, 0, 30, tzinfo=UTC)
