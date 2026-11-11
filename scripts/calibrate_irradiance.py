import argparse
import csv
import json
from dataclasses import asdict
from pathlib import Path

from pyracast.irradiance.calibration import calibrate
from pyracast.irradiance.pairs import load_pairs


def main() -> None:
    parser = argparse.ArgumentParser(description="Calibrate an offline irradiance inverse.")
    parser.add_argument("--power", type=Path, required=True)
    parser.add_argument("--weather", type=Path, required=True)
    parser.add_argument("--capacity-kw", type=float, required=True)
    parser.add_argument("--timezone", default="Europe/Istanbul")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = calibrate(
        load_pairs(args.power, args.weather), capacity_kw=args.capacity_kw, timezone=args.timezone
    )
    args.output.mkdir(parents=True, exist_ok=True)
    model = {
        "schema_version": 1,
        "model": asdict(result.model),
        "source_kind": "estimated_actuals",
        "capacity_kw": args.capacity_kw,
        "power_sha256": result.report["power_sha256"],
        "weather_sha256": result.report["weather_sha256"],
        "training": result.report["partitions"]["train"],
        "training_sample_count": result.report["fit_sample_count"],
    }
    (args.output / "model.json").write_text(
        json.dumps(model, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    (args.output / "metrics.json").write_text(
        json.dumps(result.report, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    with (args.output / "predictions.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(result.predictions[0]))
        writer.writeheader()
        for prediction in result.predictions:
            row = dict(prediction)
            row["quality_flags"] = json.dumps(row["quality_flags"])
            writer.writerow(row)
    print(
        f"Calibrated {result.report['fit_sample_count']} training intervals; "
        f"outputs in {args.output}"
    )


if __name__ == "__main__":
    main()
