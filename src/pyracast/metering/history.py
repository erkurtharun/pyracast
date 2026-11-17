from datetime import date, datetime, time, timedelta
from math import fsum, isfinite
from typing import cast
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError

from pyracast.metering import operations
from pyracast.metering.models import EpiasConnection, EpiasSnapshot, EpiasSourceKind
from pyracast.plants.models import Plant
from pyracast.plants.operations import configuration_is_current
from pyracast.solar import daylight_window


def _intervals(snapshot: EpiasSnapshot | None, day: date) -> list[dict[str, object]]:
    if snapshot is None or not snapshot.readings:
        return []
    indexed = {datetime.fromisoformat(row["provider_timestamp"]): row for row in snapshot.readings}
    midnight = datetime.combine(day, time(), ZoneInfo("Europe/Istanbul"))
    rows = []
    for hour in range(24):
        start = midnight + timedelta(hours=hour)
        reading = indexed.get(start)
        rows.append(
            {
                "source_kind": snapshot.source_kind if reading else None,
                "snapshot_id": snapshot.pk if reading else None,
                "provider_timestamp": reading["provider_timestamp"] if reading else None,
                "period_start": start,
                "period_end": start + timedelta(hours=1),
                "reported": reading is not None,
                "total_mwh": reading["total_mwh"] if reading else None,
                "sun_mwh": reading["sun_mwh"] if reading else None,
            }
        )
    return rows


def _valid_energy(row: dict[str, object]) -> bool:
    value = row["total_mwh"]
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return False
    try:
        energy = float(value)
    except ValueError, OverflowError:
        return False
    return isfinite(energy) and energy >= 0


def _automatic_intervals(
    by_source: dict[str, EpiasSnapshot], day: date
) -> tuple[list[dict[str, object]], list[EpiasSnapshot]]:
    candidates = [
        (snapshot, _intervals(snapshot, day))
        for source in reversed(EpiasSourceKind.values)
        if (snapshot := by_source.get(source)) is not None and snapshot.readings
    ]
    if not candidates:
        return [], []
    rows = []
    used: set[int] = set()
    for hour in range(24):
        for snapshot, intervals in candidates:
            row = intervals[hour]
            if _valid_energy(row):
                rows.append(row)
                used.add(snapshot.pk)
                break
        else:
            missing = next(
                (intervals[hour] for _, intervals in candidates if intervals[hour]["reported"]),
                candidates[0][1][hour],
            )
            rows.append(
                {
                    **missing,
                    "total_mwh": None,
                    "sun_mwh": None,
                    "source_kind": None,
                    "snapshot_id": None,
                }
            )
    return rows, [snapshot for snapshot, _ in candidates if snapshot.pk in used]


def _calibration_blocker(plant: Plant, snapshot: EpiasSnapshot) -> str | None:
    configuration = plant.current_configuration
    if configuration is None or snapshot.configuration_id != configuration.pk:
        return "configuration_mismatch"
    if not configuration_is_current(configuration):
        return "configuration_unavailable"
    if configuration.measurement_basis != "net_export":
        return "measurement_basis_mismatch"
    connection = EpiasConnection.objects.get(pk=snapshot.connection_id)
    if not connection.enabled:
        return "source_disconnected"
    return None


def _reported_total(
    snapshot: EpiasSnapshot | None, day: date, readings: list[dict[str, object]]
) -> float | None:
    if snapshot is None or len(readings) != 24:
        return None
    valid = [_valid_energy(row) for row in readings]
    if not all(valid):
        binding = snapshot.configuration.binding
        daylight = daylight_window(day, "Europe/Istanbul", binding.latitude, binding.longitude)
        required = [
            index
            for index, row in enumerate(readings)
            if any(
                cast(datetime, row["period_start"]) < end
                and cast(datetime, row["period_end"]) > start
                for start, end in daylight.windows
            )
        ]
        if not required or not all(valid[index] for index in required):
            return None
    try:
        return fsum(
            float(cast(str | int | float, row["total_mwh"]))
            for row, available in zip(readings, valid, strict=True)
            if available
        )
    except OverflowError:
        return None


def read_production_day(
    plant: Plant, *, actor: User, day: date, source_kind: str = "auto"
) -> dict[str, object]:
    if source_kind not in ("auto", *EpiasSourceKind.values):
        raise ValidationError("invalid_production_source")
    snapshots = operations.read_epias_history(
        plant.pk, actor=actor, start_day=day, end_day=day, source_kind=None
    )
    by_source = {snapshot.source_kind: snapshot for snapshot in snapshots}
    available = [
        source
        for source, snapshot in by_source.items()
        if any(row["total_mwh"] is not None for row in snapshot.readings)
    ]
    selected: str | None = source_kind
    if selected == "auto":
        readings, used_snapshots = _automatic_intervals(by_source, day)
        snapshot = used_snapshots[0] if used_snapshots else None
        selected = (
            "mixed"
            if len(used_snapshots) > 1
            else (snapshot.source_kind if snapshot is not None else None)
        )
    else:
        snapshot = by_source.get(source_kind)
        readings = _intervals(snapshot, day)
        used_snapshots = (
            [snapshot]
            if snapshot is not None and any(_valid_energy(row) for row in readings)
            else []
        )
    blocker = next(
        (
            reason
            for source_snapshot in used_snapshots
            if (reason := _calibration_blocker(plant, source_snapshot)) is not None
        ),
        None if used_snapshots else "production_unavailable",
    )
    return {
        "date": day,
        "timezone": "Europe/Istanbul",
        "unit": "MWh",
        "source_kind": selected,
        "complete": len(readings) == 24 and all(_valid_energy(row) for row in readings),
        "reported_total_mwh": _reported_total(snapshot, day, readings),
        "available_sources": available,
        "calibration_eligible": blocker is None,
        "calibration_blocker": blocker,
        "snapshot": snapshot,
        "source_snapshots": used_snapshots,
        "readings": readings,
    }
