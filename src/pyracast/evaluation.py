from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from itertools import pairwise
from math import fsum, isfinite


@dataclass(frozen=True)
class ForecastIdentity:
    location_id: str
    local_date: date
    configuration_revision: str
    issued_at: datetime | None
    start: datetime
    end: datetime
    provider: str = "solcast"
    source_kind: str = "forecast"


@dataclass(frozen=True)
class MatchedDailySample:
    prediction_identity: ForecastIdentity
    reference_identity: ForecastIdentity
    predicted_kwh_m2: float | None
    solcast_kwh_m2: float | None


@dataclass(frozen=True)
class MatchedIntervalSample:
    prediction_identity: ForecastIdentity
    reference_identity: ForecastIdentity
    predicted_ghi_wm2: float | None
    solcast_ghi_wm2: float | None
    daylight: bool


@dataclass(frozen=True)
class ReferenceEvaluation:
    daily_mae_kwh_m2: float | None
    daily_bias_kwh_m2: float | None
    daily_paired_count: int
    daily_sample_count: int
    daily_coverage_fraction: float | None
    daylight_mae_wm2: float | None
    daylight_bias_wm2: float | None
    daylight_valid_seconds: float
    daylight_total_seconds: float
    daylight_coverage_fraction: float | None


def _identity_key(identity: ForecastIdentity) -> tuple[object, ...]:
    if identity.provider != "solcast" or identity.source_kind != "forecast":
        raise ValueError("Only matched Solcast forecast references are supported")
    if not identity.location_id or not identity.configuration_revision:
        raise ValueError("Location and configuration revision are required")
    issue = identity.issued_at
    stamps = (identity.start, identity.end)
    if (
        issue is None
        or issue.utcoffset() is None
        or any(stamp.utcoffset() is None for stamp in stamps)
    ):
        raise ValueError("A real issue time and aware interval bounds are required")
    start, end = identity.start.astimezone(UTC), identity.end.astimezone(UTC)
    if end <= start:
        raise ValueError("Reference interval must have positive duration")
    return (
        identity.location_id,
        identity.local_date,
        identity.configuration_revision,
        issue.astimezone(UTC),
        start,
        end,
    )


def _pair(
    prediction: ForecastIdentity,
    reference: ForecastIdentity,
    predicted: float | None,
    label: float | None,
    seen: set[tuple[object, ...]],
) -> float | None:
    key = _identity_key(prediction)
    if key != _identity_key(reference):
        raise ValueError("Prediction and reference provenance do not match")
    if key in seen:
        raise ValueError("Duplicate matched samples are not supported")
    seen.add(key)
    for value in (predicted, label):
        if value is not None and (not isfinite(value) or value < 0):
            raise ValueError("Values must be finite and nonnegative, or missing")
    return None if predicted is None or label is None else predicted - label


def _mean(values: list[float], divisor: float) -> float | None:
    return fsum(value / divisor for value in values) if values else None


def evaluate_reference(
    daily_samples: Sequence[MatchedDailySample],
    intervals: Sequence[MatchedIntervalSample] = (),
) -> ReferenceEvaluation:
    """Measure prediction minus Solcast; daylight errors are weighted by interval duration."""
    daily_errors: list[float] = []
    seen_daily: set[tuple[object, ...]] = set()
    for daily in daily_samples:
        error = _pair(
            daily.prediction_identity,
            daily.reference_identity,
            daily.predicted_kwh_m2,
            daily.solcast_kwh_m2,
            seen_daily,
        )
        if error is not None:
            daily_errors.append(error)
    daylight_errors: list[tuple[float, float]] = []
    valid_seconds = total_seconds = 0.0
    seen_intervals: set[tuple[object, ...]] = set()
    windows_by_vintage: dict[tuple[object, ...], list[tuple[datetime, datetime]]] = {}
    for interval in intervals:
        error = _pair(
            interval.prediction_identity,
            interval.reference_identity,
            interval.predicted_ghi_wm2,
            interval.solcast_ghi_wm2,
            seen_intervals,
        )
        identity = interval.reference_identity
        key = _identity_key(identity)
        start, end = identity.start.astimezone(UTC), identity.end.astimezone(UTC)
        # A vintage spans target dates; different date labels cannot separate overlapping windows.
        windows_by_vintage.setdefault((key[0], key[2], key[3]), []).append((start, end))
        if not interval.daylight:
            continue
        seconds = (end - start).total_seconds()
        total_seconds += seconds
        if error is not None:
            valid_seconds += seconds
            daylight_errors.append((error, seconds))
    for windows in windows_by_vintage.values():
        if any(start < previous_end for (_, previous_end), (start, _) in pairwise(sorted(windows))):
            raise ValueError("Overlapping intervals within a forecast vintage are not supported")
    return ReferenceEvaluation(
        _mean([abs(error) for error in daily_errors], len(daily_errors)),
        _mean(daily_errors, len(daily_errors)),
        len(daily_errors),
        len(daily_samples),
        len(daily_errors) / len(daily_samples) if daily_samples else None,
        _mean([abs(error) * (seconds / valid_seconds) for error, seconds in daylight_errors], 1),
        _mean([error * (seconds / valid_seconds) for error, seconds in daylight_errors], 1),
        valid_seconds,
        total_seconds,
        valid_seconds / total_seconds if total_seconds else None,
    )
