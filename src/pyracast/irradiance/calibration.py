from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from math import fsum, sqrt
from typing import Any
from zoneinfo import ZoneInfo

from pyracast.irradiance import local_day_bounds
from pyracast.irradiance.fitting import fit_quadratic
from pyracast.irradiance.inverse import QuadraticModel, reconstruct_ghi
from pyracast.irradiance.pairs import PairedDataset, PairedInterval


@dataclass(frozen=True)
class CalibrationResult:
    report: dict[str, Any]
    model: QuadraticModel
    predictions: tuple[dict[str, Any], ...]


def _stats(errors: list[float]) -> dict[str, float | int | None]:
    n = len(errors)
    return {
        "count": n,
        "mae": fsum(abs(x) for x in errors) / n if n else None,
        "rmse": sqrt(fsum(x * x for x in errors) / n) if n else None,
        "bias": fsum(errors) / n if n else None,
    }


def _complete_days(
    samples: tuple[PairedInterval, ...], timezone: str
) -> tuple[dict[date, list[PairedInterval]], list[str]]:
    zone = ZoneInfo(timezone)
    groups: dict[date, list[PairedInterval]] = defaultdict(list)
    for sample in samples:
        groups[sample.start.astimezone(zone).date()].append(sample)
    complete: dict[date, list[PairedInterval]] = {}
    omitted = []
    for day, rows in sorted(groups.items()):
        start, end = local_day_bounds(day, timezone)
        if (
            rows[0].start == start
            and rows[-1].end == end
            and all(a.end == b.start for a, b in pairwise(rows))
        ):
            complete[day] = rows
        else:
            omitted.append(day.isoformat())
    return complete, omitted


def calibrate(
    dataset: PairedDataset, *, capacity_kw: float, timezone: str = "Europe/Istanbul"
) -> CalibrationResult:
    complete, omitted = _complete_days(dataset.samples, timezone)
    days = list(complete)
    if len(days) < 10:
        raise ValueError("At least ten complete local days are required")
    ntrain, nval = len(days) * 3 // 5, len(days) // 5
    groups = {
        "train": days[:ntrain],
        "validation": days[ntrain : ntrain + nval],
        "test": days[ntrain + nval :],
    }
    training = [sample for day in groups["train"] for sample in complete[day]]
    fit = fit_quadratic(training, capacity_kw=capacity_kw)
    model = QuadraticModel(fit.coefficients, fit.min_temperature_c, fit.max_temperature_c)
    lookup = {day: partition for partition, selected in groups.items() for day in selected}
    zone = ZoneInfo(timezone)
    predictions: list[dict[str, Any]] = []
    for sample in dataset.samples:
        day = sample.start.astimezone(zone).date()
        partition = lookup.get(day, "partial")
        estimate = reconstruct_ghi(
            sample.power_kw,
            capacity_kw=capacity_kw,
            air_temp_c=sample.air_temp_c,
            snow_loss_fraction=sample.snow_loss_fraction,
            model=model,
        )
        predicted = estimate.ghi_wm2
        identifiable = sample.power_kw > 0 and sample.ghi_wm2 > 0 and predicted is not None
        baseline = 1000 * sample.power_kw / capacity_kw
        predictions.append(
            {
                "start_utc": sample.start.isoformat(),
                "end_utc": sample.end.isoformat(),
                "local_date": day.isoformat(),
                "partition": partition,
                "reference_ghi_wm2": sample.ghi_wm2,
                "power_kw": sample.power_kw,
                "air_temp_c": sample.air_temp_c,
                "snow_loss_fraction": sample.snow_loss_fraction,
                "predicted_ghi_wm2": predicted,
                "quality_flags": list(estimate.quality_flags),
                "baseline_error_wm2": baseline - sample.ghi_wm2 if identifiable else None,
                "inverse_error_wm2": predicted - sample.ghi_wm2
                if identifiable and predicted is not None
                else None,
                "duration_hours": (sample.end - sample.start).total_seconds() / 3600,
            }
        )
    metrics: dict[str, Any] = {}
    for partition in ("train", "validation", "test"):
        rows = [row for row in predictions if row["partition"] == partition]
        paired = [row for row in rows if row["inverse_error_wm2"] is not None]
        reasons: Counter[str] = Counter()
        for row in rows:
            if row["inverse_error_wm2"] is None:
                if row["power_kw"] == 0:
                    reasons["zero_power_not_identifiable"] += 1
                elif row["reference_ghi_wm2"] <= 0:
                    reasons["nonpositive_reference_ghi"] += 1
                else:
                    reasons[row["quality_flags"][-1]] += 1
        daily: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in paired:
            daily[row["local_date"]].append(row)
        item: dict[str, Any] = {
            "interval_count": len(paired),
            "duration_hours": fsum(r["duration_hours"] for r in paired),
            "total_interval_count": len(rows),
            "total_duration_hours": fsum(r["duration_hours"] for r in rows),
            "reference_energy_kwh_m2": fsum(
                r["reference_ghi_wm2"] * r["duration_hours"] / 1000 for r in rows
            ),
            "identifiable_reference_energy_kwh_m2": fsum(
                r["reference_ghi_wm2"] * r["duration_hours"] / 1000 for r in paired
            ),
            "days_without_identifiable_intervals": len(groups[partition]) - len(daily),
            "missing_reason_counts": dict(reasons),
            "zero_power_positive_ghi_count": sum(
                r["power_kw"] == 0 and r["reference_ghi_wm2"] > 0 for r in rows
            ),
            "full_snow_count": sum(r["snow_loss_fraction"] == 1 for r in rows),
            "temperature_extrapolation_count": sum(
                "temperature_extrapolation" in r["quality_flags"] for r in rows
            ),
            "baseline_all_positive_reference": _stats(
                [
                    1000 * r["power_kw"] / capacity_kw - r["reference_ghi_wm2"]
                    for r in rows
                    if r["reference_ghi_wm2"] > 0
                ]
            ),
        }
        for name in ("baseline", "inverse"):
            key = f"{name}_error_wm2"
            item[f"{name}_interval_wm2"] = _stats([r[key] for r in paired])
            item[f"{name}_daily_partial_energy_kwh_m2"] = _stats(
                [
                    fsum(r[key] * r["duration_hours"] / 1000 for r in day_rows)
                    for day_rows in daily.values()
                ]
            )
        metrics[partition] = item
    report = {
        "metric_scope": "identifiable_intervals",
        "aggregation_limitation": (
            "The inverse uses half-hour mean weather; nonlinear five-minute variation "
            "is not recovered."
        ),
        "full_day_total_validated": False,
        "forecast_transfer_validated": False,
        "source_kind": "estimated_actuals",
        "timezone": timezone,
        "capacity_kw": capacity_kw,
        "power_sha256": dataset.power_sha256,
        "weather_sha256": dataset.weather_sha256,
        "unmatched_power_intervals": dataset.unmatched_power_intervals,
        "unmatched_weather_intervals": dataset.unmatched_weather_intervals,
        "omitted_partial_days": omitted,
        "fit_sample_count": fit.sample_count,
        "partitions": {
            name: {
                "start_date": value[0].isoformat(),
                "end_date": value[-1].isoformat(),
                "complete_day_count": len(value),
            }
            for name, value in groups.items()
        },
        "metrics": metrics,
    }
    return CalibrationResult(report, model, tuple(predictions))
