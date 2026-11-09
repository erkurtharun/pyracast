from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from pyracast.evaluation import (
    ForecastIdentity,
    MatchedDailySample,
    MatchedIntervalSample,
    evaluate_reference,
)

ISSUE = datetime(2026, 11, 9, 12, tzinfo=UTC)
START = datetime(2026, 11, 10, tzinfo=UTC)


def identity(location: str = "location-1") -> ForecastIdentity:
    return ForecastIdentity(
        location, date(2026, 11, 10), "binding-v1", ISSUE, START, START + timedelta(days=1)
    )


def test_daily_metrics_use_matched_labels_and_report_missing_pairs_separately() -> None:
    values = ((2, 4), (6, 2), (0, 0), (None, 1), (2, None))
    samples = [
        MatchedDailySample(identity(str(index)), identity(str(index)), predicted, label)
        for index, (predicted, label) in enumerate(values)
    ]
    result = evaluate_reference(samples)
    assert result.daily_mae_kwh_m2 == 2
    assert result.daily_bias_kwh_m2 == pytest.approx(2 / 3)
    assert result.daily_paired_count == 3
    assert result.daily_sample_count == 5
    assert result.daily_coverage_fraction == 0.6
    assert result.daylight_mae_wm2 is None
    assert result.daylight_bias_wm2 is None
    assert result.daylight_total_seconds == result.daylight_valid_seconds == 0
    assert result.daylight_coverage_fraction is None


def test_daylight_error_is_duration_weighted_and_night_is_excluded() -> None:
    first = replace(identity(), end=START + timedelta(minutes=30))
    second = replace(identity(), start=first.end, end=first.end + timedelta(hours=1))
    missing = replace(identity(), start=second.end, end=second.end + timedelta(minutes=30))
    night = replace(identity(), start=missing.end)
    result = evaluate_reference(
        [],
        [
            MatchedIntervalSample(first, first, 600, 400, True),
            MatchedIntervalSample(second, second, 200, 300, True),
            MatchedIntervalSample(missing, missing, None, 300, True),
            MatchedIntervalSample(night, night, None, None, False),
        ],
    )
    assert result.daily_mae_kwh_m2 is None
    assert result.daily_bias_kwh_m2 is None
    assert result.daily_coverage_fraction is None
    assert result.daylight_mae_wm2 == pytest.approx(400 / 3)
    assert result.daylight_bias_wm2 == 0
    assert result.daylight_valid_seconds == 5400
    assert result.daylight_total_seconds == 7200
    assert result.daylight_coverage_fraction == 0.75


def test_zero_labels_are_valid_and_missing_daylight_never_becomes_zero_error() -> None:
    source = identity()
    zero = evaluate_reference([MatchedDailySample(source, source, 0, 0)])
    assert zero.daily_mae_kwh_m2 == zero.daily_bias_kwh_m2 == 0
    missing = evaluate_reference([], [MatchedIntervalSample(source, source, 0, None, True)])
    assert missing.daylight_mae_wm2 is None
    assert missing.daylight_bias_wm2 is None
    assert missing.daylight_coverage_fraction == 0


def test_equivalent_utc_instants_match_without_changing_issue_time() -> None:
    source = identity()
    zone = timezone(timedelta(hours=3))
    reference = replace(
        source,
        issued_at=ISSUE.astimezone(zone),
        start=source.start.astimezone(zone),
        end=source.end.astimezone(zone),
    )
    assert evaluate_reference([MatchedDailySample(source, reference, 1, 2)]).daily_bias_kwh_m2 == -1


@pytest.mark.parametrize(
    "reference",
    [
        replace(identity(), location_id="other-location"),
        replace(identity(), local_date=date(2026, 11, 11)),
        replace(identity(), configuration_revision="other-binding"),
        replace(identity(), issued_at=ISSUE + timedelta(hours=1)),
        replace(identity(), start=START + timedelta(minutes=30)),
        replace(identity(), end=START + timedelta(hours=23)),
    ],
)
def test_mismatched_provenance_is_rejected(reference: ForecastIdentity) -> None:
    with pytest.raises(ValueError, match="provenance"):
        evaluate_reference([MatchedDailySample(identity(), reference, 1, 2)])


@pytest.mark.parametrize(
    "source",
    [
        replace(identity(), provider="satellite"),
        replace(identity(), source_kind="estimated_actuals"),
        replace(identity(), location_id=""),
        replace(identity(), configuration_revision=""),
        replace(identity(), issued_at=None),
        replace(identity(), issued_at=ISSUE.replace(tzinfo=None)),
        replace(identity(), start=START.replace(tzinfo=None)),
        replace(identity(), end=START.replace(tzinfo=None)),
        replace(identity(), end=START),
        replace(identity(), end=START - timedelta(hours=1)),
    ],
)
def test_unverified_or_invalid_reference_identity_is_rejected(source: ForecastIdentity) -> None:
    with pytest.raises(ValueError, match=r"Solcast|revision|required|duration"):
        evaluate_reference([MatchedDailySample(source, source, 1, 2)])


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
@pytest.mark.parametrize("invalid_prediction", [True, False])
def test_nonfinite_or_negative_values_are_explicit_errors(
    value: float, invalid_prediction: bool
) -> None:
    predicted, label = (value, 1) if invalid_prediction else (1, value)
    with pytest.raises(ValueError, match="finite and nonnegative"):
        evaluate_reference([MatchedDailySample(identity(), identity(), predicted, label)])


def test_duplicate_daily_and_interval_samples_are_rejected() -> None:
    daily = MatchedDailySample(identity(), identity(), 1, 2)
    with pytest.raises(ValueError, match="Duplicate"):
        evaluate_reference([daily, daily])
    interval = MatchedIntervalSample(identity(), identity(), 100, 200, True)
    with pytest.raises(ValueError, match="Duplicate"):
        evaluate_reference([], [interval, interval])


def test_large_finite_values_do_not_overflow_metric_accumulation() -> None:
    first, second = identity("first"), identity("second")
    result = evaluate_reference(
        [MatchedDailySample(first, first, 1e308, 0), MatchedDailySample(second, second, 1e308, 0)],
        [
            MatchedIntervalSample(first, first, 1e308, 0, True),
            MatchedIntervalSample(second, second, 1e308, 0, True),
        ],
    )
    assert result.daily_mae_kwh_m2 == result.daily_bias_kwh_m2 == 1e308
    assert result.daylight_mae_wm2 == result.daylight_bias_wm2 == 1e308
