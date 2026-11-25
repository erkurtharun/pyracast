from datetime import UTC, date, datetime, timedelta

import pytest

from pyracast.forecasts.coverage import history_day_complete
from pyracast.forecasts.faults import DamagedStoredDataError
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


def _whole_day(power: object) -> ForecastSnapshot:
    return ForecastSnapshot(
        pk=7,
        normalized_intervals=[
            {
                "start": "2026-11-08T00:00:00+00:00",
                "end": "2026-11-09T00:00:00+00:00",
                "power_kw": power,
            }
        ],
    )


@pytest.mark.parametrize("power", [None, -1])
def test_a_missing_or_negative_power_leaves_the_day_incomplete(power: object) -> None:
    assert not history_day_complete(_whole_day(power), date(2026, 11, 8), "UTC")


@pytest.mark.parametrize(
    ("power", "fault"),
    [
        (float("nan"), "ValueError"),
        (True, "ValueError"),
        ("missing", "ValueError"),
        (10**400, "OverflowError"),
    ],
    ids=["nan", "boolean", "text", "beyond-a-float"],
)
def test_a_power_that_is_not_a_representable_number_is_damage_of_the_snapshot(
    power: object, fault: str
) -> None:
    with pytest.raises(DamagedStoredDataError) as damage:
        history_day_complete(_whole_day(power), date(2026, 11, 8), "UTC")
    assert (damage.value.kind, damage.value.identity, damage.value.fault) == (
        "forecast_snapshot",
        7,
        fault,
    )


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
def test_a_malformed_or_naive_interval_is_damage_of_the_snapshot(malformed: object) -> None:
    snapshot = ForecastSnapshot(
        pk=7,
        normalized_intervals=[
            {
                "start": "2026-11-08T00:00:00+00:00",
                "end": "2026-11-09T00:00:00+00:00",
                "power_kw": 0,
            },
            malformed,
        ],
    )
    with pytest.raises(DamagedStoredDataError) as damage:
        history_day_complete(snapshot, date(2026, 11, 8), "UTC")
    assert damage.value.identity == 7


@pytest.mark.parametrize("stored", [5, "rows", {"start": "2026-11-08T00:00:00+00:00"}, None])
def test_stored_intervals_that_are_not_an_array_are_damage_of_the_snapshot(stored: object) -> None:
    with pytest.raises(DamagedStoredDataError) as damage:
        history_day_complete(
            ForecastSnapshot(pk=7, normalized_intervals=stored), date(2026, 11, 8), "UTC"
        )
    assert (damage.value.identity, damage.value.fault) == (7, "TypeError")
