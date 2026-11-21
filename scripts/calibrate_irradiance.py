import argparse
import csv
import json
import math
import os
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pyracast.irradiance.calibration import CalibrationResult, calibrate
from pyracast.irradiance.pairs import load_pairs

OUTPUTS = ("model.json", "metrics.json", "predictions.csv")


def _capacity(value: str) -> float:
    try:
        capacity = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number") from None
    if not math.isfinite(capacity) or capacity <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number of kW")
    return capacity


def _timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ValueError, ZoneInfoNotFoundError:
        raise argparse.ArgumentTypeError("must be an IANA timezone") from None
    return value


def _write(directory: Path, result: CalibrationResult, capacity_kw: float) -> None:
    model = {
        "schema_version": 1,
        "model": asdict(result.model),
        "source_kind": "estimated_actuals",
        "capacity_kw": capacity_kw,
        "power_sha256": result.report["power_sha256"],
        "weather_sha256": result.report["weather_sha256"],
        "training": result.report["partitions"]["train"],
        "training_sample_count": result.report["fit_sample_count"],
    }
    (directory / "model.json").write_text(
        json.dumps(model, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    (directory / "metrics.json").write_text(
        json.dumps(result.report, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    with (directory / "predictions.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(result.predictions[0]))
        writer.writeheader()
        for prediction in result.predictions:
            row = dict(prediction)
            row["quality_flags"] = json.dumps(row["quality_flags"])
            writer.writerow(row)


def _publish(output: Path, result: CalibrationResult, capacity_kw: float) -> None:
    """Writes every file beside the output first, so an interrupted run leaves no mix."""
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        _write(staging, result, capacity_kw)
        for name in OUTPUTS:
            os.replace(staging / name, output / name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Calibrate an offline irradiance inverse.")
    parser.add_argument("--power", type=Path, required=True)
    parser.add_argument("--weather", type=Path, required=True)
    parser.add_argument("--capacity-kw", type=_capacity, required=True)
    parser.add_argument("--timezone", type=_timezone, default="Europe/Istanbul")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true", help="Replace existing outputs.")
    args = parser.parse_args(argv)
    existing = [name for name in OUTPUTS if (args.output / name).exists()]
    if existing and not args.overwrite:
        parser.error(f"{args.output} already holds {', '.join(existing)}; pass --overwrite.")
    try:
        result = calibrate(
            load_pairs(args.power, args.weather),
            capacity_kw=args.capacity_kw,
            timezone=args.timezone,
        )
    except ValueError as error:
        parser.error(str(error))
    _publish(args.output, result, args.capacity_kw)
    print(
        f"Calibrated {result.report['fit_sample_count']} training intervals from "
        f"{sum(item['complete_day_count'] for item in result.report['partitions'].values())} "
        f"complete days; outputs in {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
