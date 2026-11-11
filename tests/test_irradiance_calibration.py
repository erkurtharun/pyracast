import csv
import json
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pyracast.irradiance.calibration import calibrate
from pyracast.irradiance.pairs import PairedDataset, PairedInterval, WeatherInterval

START = datetime(2026, 1, 1, tzinfo=UTC)


def dataset(days: int = 11, *, holdout_add: float = 0) -> PairedDataset:
    samples = []
    for index in range(days * 48):
        start = START + timedelta(minutes=30 * index)
        g = 100 + (index * 71 % 800)
        t = -5 + (index * 37 % 300) / 10
        p = 1000 * (
            0.01
            + 0.85 * g / 1000
            + 0.02 * (t - 25) / 25
            + 0.015 * g / 1000 * (t - 25) / 25
            + 0.02 * (g / 1000) ** 2
            + 0.01 * ((t - 25) / 25) ** 2
        )
        if index >= 9 * 48:
            p += holdout_add
        weather = tuple(
            WeatherInterval(
                start + timedelta(minutes=5 * i), start + timedelta(minutes=5 * (i + 1)), g, t, 0
            )
            for i in range(6)
        )
        samples.append(PairedInterval(start, start + timedelta(minutes=30), p, g, t, 0, weather))
    return PairedDataset(tuple(samples), 0, 0, "powerhash", "weatherhash")


def test_chronological_split_no_holdout_refit_and_partial_coverage() -> None:
    original = dataset()
    trimmed = PairedDataset(original.samples[1:], 1, 2, "p", "w")
    result = calibrate(trimmed, capacity_kw=1000, timezone="UTC")
    report = result.report
    assert report["omitted_partial_days"] == ["2026-01-01"]
    assert [
        report["partitions"][x]["complete_day_count"] for x in ("train", "validation", "test")
    ] == [6, 2, 2]
    assert report["metric_scope"] == "identifiable_intervals"
    assert report["full_day_total_validated"] is report["forecast_transfer_validated"] is False
    assert (report["unmatched_power_intervals"], report["unmatched_weather_intervals"]) == (1, 2)
    assert result.predictions[0]["partition"] == "partial"
    assert calibrate(
        dataset(11, holdout_add=40), capacity_kw=1000, timezone="UTC"
    ).model.coefficients == pytest.approx(
        calibrate(original, capacity_kw=1000, timezone="UTC").model.coefficients
    )


def test_insufficient_and_gapped_days_fail() -> None:
    original = dataset(10)
    with pytest.raises(ValueError, match="ten complete"):
        calibrate(
            PairedDataset(original.samples[:50] + original.samples[51:], 0, 0, "", ""),
            capacity_kw=1000,
            timezone="UTC",
        )


def test_rank_deficient_training_fails() -> None:
    original = dataset(10)
    repeated = tuple(
        PairedInterval(
            s.start,
            s.end,
            400,
            500,
            20,
            0,
            tuple(WeatherInterval(w.start, w.end, 500, 20, 0) for w in s.weather),
        )
        for s in original.samples
    )
    with pytest.raises(ValueError, match="rank deficient"):
        calibrate(PairedDataset(repeated, 0, 0, "", ""), capacity_kw=1000, timezone="UTC")


def test_cli_writes_model_metrics_and_csv(tmp_path: Path) -> None:
    data = dataset(11)
    power, weather = [], []
    for sample in data.samples:
        power.append(
            {
                "period_end": sample.end.isoformat(),
                "period": "PT30M",
                "pv_power_rooftop": sample.power_kw,
            }
        )
        for row in sample.weather:
            weather.append(
                {
                    "period_end": row.end.isoformat(),
                    "period": "PT5M",
                    "ghi": row.ghi_wm2,
                    "air_temp": row.air_temp_c,
                    "snow_soiling_rooftop": 0,
                }
            )
    power[0]["pv_power_rooftop"] = 0
    paths = [tmp_path / "power.json", tmp_path / "weather.json"]
    for path, rows in zip(paths, (power, weather), strict=True):
        path.write_text(json.dumps({"estimated_actuals": rows}), encoding="utf-8")
    output = tmp_path / "out"
    subprocess.run(  # noqa: S603
        [
            sys.executable,
            "scripts/calibrate_irradiance.py",
            "--power",
            str(paths[0]),
            "--weather",
            str(paths[1]),
            "--capacity-kw",
            "1000",
            "--timezone",
            "UTC",
            "--output",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    model = json.loads((output / "model.json").read_text(encoding="utf-8"))
    metrics = json.loads((output / "metrics.json").read_text(encoding="utf-8"))
    assert model["source_kind"] == "estimated_actuals"
    assert model["power_sha256"] == metrics["power_sha256"]
    with (output / "predictions.csv").open(encoding="utf-8", newline="") as stream:
        csv_row = next(csv.DictReader(stream))
    assert csv_row["inverse_error_wm2"] == ""
    assert csv_row["partition"] == "train"


def test_non_identifiable_holdout_reports_empty_metrics() -> None:
    original = dataset(10)
    samples = tuple(
        replace(sample, power_kw=0) if i >= 8 * 48 else sample
        for i, sample in enumerate(original.samples)
    )
    result = calibrate(replace(original, samples=samples), capacity_kw=1000, timezone="UTC")
    metrics = result.report["metrics"]["test"]
    assert metrics["zero_power_positive_ghi_count"] == 96
    assert metrics["interval_count"] == 0
    assert metrics["inverse_interval_wm2"]["mae"] is None
    assert sum(metrics["missing_reason_counts"].values()) >= 96


def test_missing_reasons_distinguish_zero_reference_and_inverse_failure() -> None:
    original = dataset(10)
    samples = list(original.samples)
    samples[-1] = replace(samples[-1], ghi_wm2=0)
    samples[-2] = replace(samples[-2], snow_loss_fraction=1)
    samples[-3] = replace(samples[-3], air_temp_c=50)
    result = calibrate(replace(original, samples=tuple(samples)), capacity_kw=1000, timezone="UTC")
    reasons = result.report["metrics"]["test"]["missing_reason_counts"]
    assert reasons["nonpositive_reference_ghi"] == 1
    assert reasons["snow_opaque"] == 1
    assert result.report["metrics"]["test"]["temperature_extrapolation_count"] == 1


def test_complete_day_selection_preserves_a_daylight_saving_short_day() -> None:
    original = dataset(11)
    shift = datetime(2026, 3, 19, 23, tzinfo=UTC) - START
    samples = tuple(
        replace(
            sample,
            start=sample.start + shift,
            end=sample.end + shift,
            weather=tuple(
                replace(row, start=row.start + shift, end=row.end + shift) for row in sample.weather
            ),
        )
        for sample in original.samples[:-2]
    )
    result = calibrate(
        replace(original, samples=samples), capacity_kw=1000, timezone="Europe/Berlin"
    )
    assert result.report["omitted_partial_days"] == []
    assert sum(item["total_duration_hours"] for item in result.report["metrics"].values()) == 263
    assert result.report["metrics"]["test"]["total_interval_count"] == 142
