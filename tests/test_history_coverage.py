from datetime import UTC, date, datetime, timedelta

import pytest

from pyracast.forecasts.coverage import history_day_complete
from pyracast.forecasts.models import ForecastSnapshot


@pytest.mark.parametrize(
    ("day", "start", "hours"),
    [
        (date(2026, 3, 29), datetime(2026, 3, 28, 23, tzinfo=UTC), 23),
        (date(2026, 10, 25), datetime(2026, 10, 24, 22, tzinfo=UTC), 25),
    ],
)
def test_history_coverage_requires_actual_dst_day_duration(
    day: date, start: datetime, hours: int
) -> None:
    rows: list[dict[str, object]] = [
        {
            "start": (start + timedelta(minutes=30 * i)).isoformat(),
            "end": (start + timedelta(minutes=30 * (i + 1))).isoformat(),
            "power_kw": 0,
        }
        for i in range(hours * 2)
    ]
    snapshot = ForecastSnapshot(normalized_intervals=rows)
    assert history_day_complete(snapshot, day, "Europe/Berlin")
    snapshot.normalized_intervals = rows[:-1]
    assert not history_day_complete(snapshot, day, "Europe/Berlin")


@pytest.mark.parametrize("power", [None, -1, float("nan"), True, "missing"])
def test_history_coverage_rejects_invalid_raw_power(power: object) -> None:
    snapshot = ForecastSnapshot(
        normalized_intervals=[
            {
                "start": "2026-11-08T00:00:00+00:00",
                "end": "2026-11-09T00:00:00+00:00",
                "power_kw": power,
            }
        ]
    )
    assert not history_day_complete(snapshot, date(2026, 11, 8), "UTC")


def test_history_coverage_rejects_duplicate_interval_straddling_local_midnight() -> None:
    start = datetime(2026, 11, 9, 18, tzinfo=UTC)
    rows: list[dict[str, object]] = [
        {
            "start": (start + timedelta(minutes=30 * i)).isoformat(),
            "end": (start + timedelta(minutes=30 * (i + 1))).isoformat(),
            "power_kw": 0,
        }
        for i in range(49)
    ]
    snapshot = ForecastSnapshot(normalized_intervals=rows)
    assert history_day_complete(snapshot, date(2026, 11, 10), "Asia/Kathmandu")
    snapshot.normalized_intervals = [rows[0], *rows]
    assert not history_day_complete(snapshot, date(2026, 11, 10), "Asia/Kathmandu")


@pytest.mark.parametrize(
    "malformed",
    [
        {"start": None, "end": "2026-11-09T00:00:00+00:00"},
        {"start": 123, "end": "2026-11-09T00:00:00+00:00"},
        {"start": "invalid", "end": "2026-11-09T00:00:00+00:00"},
        {"start": "2026-11-08T00:00:00", "end": "2026-11-09T00:00:00+00:00"},
        {"start": "2026-11-08T00:00:00+00:00", "end": "2026-11-09T00:00:00"},
        {"start": "2026-11-08T00:00:00+00:00", "end": []},
        {"start": "2026-11-08T00:00:00+00:00", "end": "invalid"},
        {"start": "2026-11-08T00:00:00+00:00"},
        {"end": "2026-11-09T00:00:00+00:00"},
        "invalid row",
    ],
)
def test_history_coverage_rejects_malformed_or_naive_timestamps(malformed: object) -> None:
    snapshot = ForecastSnapshot(
        normalized_intervals=[
            {
                "start": "2026-11-08T00:00:00+00:00",
                "end": "2026-11-09T00:00:00+00:00",
                "power_kw": 0,
            },
            malformed,
        ]
    )
    assert not history_day_complete(snapshot, date(2026, 11, 8), "UTC")
