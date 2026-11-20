from typing import Any

DAY_FIELDS = (
    "status",
    "reason",
    "factor",
    "measured_energy_mwh",
    "predicted_energy_mwh",
    "baseline_energy_mwh",
    "error_mwh",
    "source_kind",
    "historical_configuration_assumed",
    "provisional_production",
)
SUMMARY_FIELDS = (
    "matched_days",
    "missing_days",
    "excluded_days",
    "scored_intervals",
    "scored_hours",
    "metrics",
    "baseline_metrics",
)


def performance_numbers(
    result: dict[str, Any], snapshots: dict[int, str], calibrations: dict[int, str]
) -> dict[str, Any]:
    """Every number and selection of a performance read, with database ids as fixture labels."""
    return {
        **{field: result[field] for field in SUMMARY_FIELDS},
        "days": [
            {
                "date": str(day["date"]),
                **{field: day[field] for field in DAY_FIELDS},
                "snapshot": snapshots.get(day["snapshot_id"]),
                "calibration": calibrations.get(day["calibration_id"]),
                "sources": [
                    snapshots[identifier] for identifier in day.get("source_snapshot_ids", [])
                ],
            }
            for day in result["days"]
        ],
    }
