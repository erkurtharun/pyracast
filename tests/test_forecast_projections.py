from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from json import dumps, loads
from typing import cast

import pytest

from pyracast.calculation import calculate
from pyracast.connections.contracts import ProviderData
from pyracast.connections.models import ProviderAccount, SiteBinding
from pyracast.forecasts.models import CollectionJob, ForecastResult, ForecastSnapshot
from pyracast.forecasts.projections import prepare_daily_payloads, saved_day_payload
from pyracast.intervals import NormalizedInterval
from pyracast.irradiance import local_day_bounds
from tests.support.domain import NOW, FakeAdapter, make_system
from tests.support.jobs import collect


def source_data() -> ProviderData:
    return ProviderData(
        {},
        NormalizedInterval.from_rows(
            [
                {
                    "start": (NOW + timedelta(hours=hour)).isoformat(),
                    "end": (NOW + timedelta(hours=hour + 1)).isoformat(),
                    "power_kw": 0.5,
                }
                for hour in range(72)
            ]
        ),
        collected_at=NOW,
    )


def saved_snapshot() -> ForecastSnapshot:
    binding = SiteBinding(
        pk=1,
        region_id=1,
        provider_account=ProviderAccount(provider="fixture"),
        region_revision=1,
        capacity_kw=1,
        capacity_semantics_verified=True,
        latitude=37.80375,
        longitude=34.460913,
        timezone="Europe/Istanbul",
    )
    snapshot = ForecastSnapshot(binding=binding, collected_at=NOW, source_kind="forecast")
    target = date(2026, 11, 10)
    data = source_data()
    payload = calculate(binding, target, data)
    ForecastResult(
        snapshot=snapshot,
        payload=payload,
        daily_payloads=prepare_daily_payloads(binding, target, data, calculate, payload),
        published_at=NOW,
    )
    return snapshot


def test_saved_projection_survives_calculator_failure_after_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = saved_snapshot()

    def fail(*args: object) -> dict[str, object]:
        raise ArithmeticError("The numerical model is temporarily unavailable.")

    monkeypatch.setattr("pyracast.calculation.calculate", fail)
    assert set(snapshot.forecastresult.daily_payloads) == {
        "2026-11-09",
        "2026-11-10",
        "2026-11-11",
    }
    result = saved_day_payload(snapshot, date(2026, 11, 11))
    assert result is not None
    assert result["energy_kwh_m2"] == 12
    assert result["valid_coverage_seconds"] == 86400
    assert result["complete"] is True
    assert saved_day_payload(snapshot, date(2026, 11, 12)) is None
    assert saved_day_payload(snapshot, date(2026, 11, 13)) is None


def test_supplemental_failure_preserves_other_days_and_bounds_the_horizon() -> None:
    snapshot = saved_snapshot()
    data = source_data()
    data = replace(
        data,
        intervals=(
            *data.intervals,
            NormalizedInterval(NOW, NOW, None),
            NormalizedInterval(NOW + timedelta(days=30), NOW + timedelta(days=31), None),
        ),
    )

    def project(binding: SiteBinding, day: date, data: ProviderData) -> dict[str, object]:
        if day == date(2026, 11, 11):
            raise ValueError("One day cannot be calculated.")
        return {"local_date": day.isoformat()}

    results = prepare_daily_payloads(
        snapshot.binding,
        date(2026, 11, 10),
        data,
        project,
        snapshot.forecastresult.payload,
    )
    assert set(results) == {"2026-11-09", "2026-11-10"}
    assert results["2026-11-10"]["energy_kwh_m2"] == 12
    assert prepare_daily_payloads(
        snapshot.binding, date(2026, 11, 10), ProviderData({}, ()), project, {}
    ) == {"2026-11-10": {}}


def test_legacy_projection_is_used_only_for_its_original_date() -> None:
    snapshot = saved_snapshot()
    snapshot.forecastresult.daily_payloads = {}
    assert saved_day_payload(snapshot, date(2026, 11, 10)) is not None
    assert saved_day_payload(snapshot, date(2026, 11, 11)) is None
    snapshot.forecastresult.daily_payloads = []
    assert saved_day_payload(snapshot, date(2026, 11, 10)) is not None
    assert saved_day_payload(ForecastSnapshot(binding=snapshot.binding), date(2026, 11, 10)) is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("local_date", "2026-11-12"),
        ("timezone", "UTC"),
        ("region_id", 2),
        ("complete", "true"),
        ("model_version", None),
        ("model_version", ""),
        ("quality_flags", None),
        ("quality_flags", [1]),
        ("intervals", None),
        ("intervals", []),
        ("start", None),
        ("start", "invalid"),
        ("start", "2026-11-11T00:00:00"),
        ("start", (NOW + timedelta(days=5)).isoformat()),
        ("end", NOW.isoformat()),
        ("valid_coverage_seconds", True),
        ("valid_coverage_seconds", "86400"),
        ("valid_coverage_seconds", float("nan")),
        ("valid_coverage_seconds", -1),
        ("valid_coverage_seconds", 90000),
        ("valid_coverage_seconds", 80000),
        ("coverage_seconds", 90000),
        ("coverage_fraction", 2),
        ("coverage_fraction", 0.5),
        ("partial_energy_kwh_m2", float("inf")),
        ("energy_kwh_m2", None),
        ("energy_kwh_m2", 13),
        ("vintage", None),
        ("vintage", {"binding_revision": "another-binding"}),
        ("vintage", {"binding_revision": "1:1", "source_kind": "estimated_actuals"}),
        ("complete", False),
        ("intervals", [None]),
        ("intervals", [{"quality_flags": None}]),
    ],
)
def test_invalid_cached_days_are_unavailable(key: str, value: object) -> None:
    snapshot = saved_snapshot()
    snapshot.forecastresult.daily_payloads["2026-11-11"][key] = value
    assert saved_day_payload(snapshot, date(2026, 11, 11)) is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("start", (NOW + timedelta(days=5)).isoformat()),
        ("end", NOW.isoformat()),
        ("ghi_wm2", float("nan")),
        ("quality_flags", [False]),
    ],
)
def test_invalid_interval_is_not_delivered(key: str, value: object) -> None:
    snapshot = saved_snapshot()
    snapshot.forecastresult.daily_payloads["2026-11-11"]["intervals"][0][key] = value
    assert saved_day_payload(snapshot, date(2026, 11, 11)) is None


def test_missing_irradiance_retains_a_valid_partial_result() -> None:
    snapshot = saved_snapshot()
    data = source_data()
    data = replace(
        data,
        intervals=(
            *data.intervals[:20],
            replace(data.intervals[20], power_kw=None),
            *data.intervals[21:],
        ),
    )
    day = date(2026, 11, 10)
    snapshot.forecastresult.daily_payloads[day.isoformat()] = calculate(snapshot.binding, day, data)
    result = saved_day_payload(snapshot, day)
    assert result is not None
    assert result["complete"] is False
    assert result["energy_kwh_m2"] is None
    assert result["partial_energy_kwh_m2"] == 11.5
    snapshot.forecastresult.daily_payloads[day.isoformat()]["valid_coverage_seconds"] = 80000
    assert saved_day_payload(snapshot, day) == snapshot.forecastresult.payload
    snapshot.forecastresult.payload = {}
    assert saved_day_payload(snapshot, day) is None


def test_partial_coverage_fraction_must_match_saved_intervals() -> None:
    snapshot = saved_snapshot()
    day = date(2026, 11, 9)
    snapshot.forecastresult.daily_payloads[day.isoformat()]["coverage_fraction"] = 0.1
    assert saved_day_payload(snapshot, day) is None


def test_a_full_day_cannot_be_mislabeled_as_partial() -> None:
    snapshot = saved_snapshot()
    day = date(2026, 11, 11)
    snapshot.forecastresult.daily_payloads[day.isoformat()]["complete"] = False
    snapshot.forecastresult.daily_payloads[day.isoformat()]["energy_kwh_m2"] = None
    assert saved_day_payload(snapshot, day) is None


@pytest.mark.parametrize("day", [date(2026, 11, 11), date(2026, 11, 9)])
def test_saved_summary_must_match_the_energy_in_its_interval_csv(day: date) -> None:
    snapshot = saved_snapshot()
    payload = snapshot.forecastresult.daily_payloads[day.isoformat()]
    payload["partial_energy_kwh_m2"] = 0
    if payload["complete"]:
        payload["energy_kwh_m2"] = 0
    assert saved_day_payload(snapshot, day) is None


def test_native_intervals_may_cross_local_day_boundaries() -> None:
    snapshot = saved_snapshot()
    snapshot.binding.timezone = "Asia/Kathmandu"
    day = date(2026, 11, 10)
    snapshot.forecastresult.daily_payloads[day.isoformat()] = calculate(
        snapshot.binding, day, source_data()
    )
    result = saved_day_payload(snapshot, day)
    assert result is not None
    assert result["energy_kwh_m2"] == 12
    assert "prorated_interval_mean" in cast(list[str], result["quality_flags"])


def test_saved_day_validation_preserves_dst_day_length() -> None:
    snapshot = saved_snapshot()
    snapshot.binding.timezone = "Europe/London"
    start = datetime(2026, 10, 24, 23, tzinfo=UTC)
    data = ProviderData(
        {},
        NormalizedInterval.from_rows(
            [
                {
                    "start": (start + timedelta(hours=hour)).isoformat(),
                    "end": (start + timedelta(hours=hour + 1)).isoformat(),
                    "power_kw": 0.5,
                }
                for hour in range(25)
            ]
        ),
        collected_at=start,
    )
    day = date(2026, 10, 25)
    snapshot.forecastresult.daily_payloads[day.isoformat()] = calculate(snapshot.binding, day, data)
    result = saved_day_payload(snapshot, day)
    assert result is not None
    assert result["energy_kwh_m2"] == 12.5
    assert result["valid_coverage_seconds"] == 90000


@pytest.mark.parametrize(
    ("zone", "day", "hours"),
    [
        ("Europe/London", date(2026, 3, 29), 23),
        ("Europe/London", date(2026, 10, 25), 25),
        ("Asia/Kathmandu", date(2026, 11, 11), 24),
    ],
)
@pytest.mark.parametrize("missing", [False, True])
def test_nonround_calculated_intervals_survive_saved_json_validation(
    zone: str,
    day: date,
    hours: int,
    missing: bool,
) -> None:
    snapshot = saved_snapshot()
    snapshot.binding.timezone = zone
    start, end = local_day_bounds(day, zone)
    left = start - timedelta(minutes=17, microseconds=321)
    intervals: list[NormalizedInterval] = []
    first = left
    while left < end:
        right = left + timedelta(minutes=37, seconds=13, microseconds=123456)
        index = len(intervals)
        intervals.append(
            NormalizedInterval(
                left, right, None if missing and index == 3 else 0.123456789 + index / 311
            )
        )
        left = right
    payload = calculate(
        snapshot.binding, day, ProviderData({}, tuple(intervals), collected_at=first)
    )
    snapshot.forecastresult.daily_payloads = {day.isoformat(): loads(dumps(payload))}
    snapshot.forecastresult.payload = {}
    result = saved_day_payload(snapshot, day)
    assert result == payload
    assert result is not None
    assert result["complete"] is not missing
    assert result["coverage_seconds"] == pytest.approx(hours * 3600)
    assert (result["energy_kwh_m2"] is None) is missing
    assert "prorated_interval_mean" in cast(list[str], result["quality_flags"])


def test_spring_clock_change_still_prepares_only_three_local_dates() -> None:
    snapshot = saved_snapshot()
    snapshot.binding.timezone = "Europe/London"
    start = datetime(2026, 3, 27, 23, 30, tzinfo=UTC)
    data = ProviderData(
        {},
        NormalizedInterval.from_rows(
            [{"start": start.isoformat(), "end": (start + timedelta(hours=72)).isoformat()}]
        ),
        collected_at=start,
    )

    def project(binding: SiteBinding, day: date, data: ProviderData) -> dict[str, object]:
        return {"local_date": day.isoformat()}

    results = prepare_daily_payloads(snapshot.binding, date(2026, 3, 28), data, project, {})
    assert set(results) == {"2026-03-27", "2026-03-28", "2026-03-29"}


@pytest.mark.django_db(transaction=True)
def test_collection_publishes_supplemental_days_atomically() -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()

    class Adapter(FakeAdapter):
        def fetch(self, binding: SiteBinding, target_date: object) -> ProviderData:
            return source_data()

    assert collect(job.pk, Adapter(), calculate, NOW)
    snapshot = ForecastSnapshot.objects.get()
    result = saved_day_payload(snapshot, job.target_date + timedelta(days=1))
    assert result is not None
    assert result["energy_kwh_m2"] == 12
    assert ForecastResult.objects.get().payload["local_date"] == job.target_date.isoformat()
    assert not collect(job.pk, Adapter(), calculate, NOW)
    assert ForecastResult.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_supplemental_solver_crash_does_not_discard_the_valid_target_day() -> None:
    make_system(approved=True)
    job = CollectionJob.objects.get()

    class Adapter(FakeAdapter):
        def fetch(self, binding: SiteBinding, target_date: object) -> ProviderData:
            return source_data()

    def project(binding: SiteBinding, target: date, data: ProviderData) -> dict[str, object]:
        if target == job.target_date + timedelta(days=1):
            raise RuntimeError("An unexpected numerical model failure.")
        return calculate(binding, target, data)

    assert collect(job.pk, Adapter(), project, NOW)
    snapshot = ForecastSnapshot.objects.get()
    result = saved_day_payload(snapshot, job.target_date)
    assert result is not None
    assert result["energy_kwh_m2"] == 12
    assert saved_day_payload(snapshot, job.target_date + timedelta(days=1)) is None
