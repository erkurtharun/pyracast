import hashlib
import json
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

from django.db.models import QuerySet

from pyracast.epias import TIMEZONE
from pyracast.forecasts.history import select_actual_snapshot
from pyracast.incidents.operations import pinned_incidents, select_incidents
from pyracast.irradiance import local_day_bounds
from pyracast.metering.models import EpiasConnection, EpiasSnapshot
from pyracast.plants.models import CalibrationRun, PlantConfiguration, ProductionObservation

MEASUREMENT_POLICY = "manual-over-settlement-hour-v1"


@dataclass(frozen=True)
class Measurement:
    starts_at: datetime
    ends_at: datetime
    energy_mwh: float
    quality: str
    source: str
    pk: int | None
    recorded_at: datetime
    snapshot: EpiasSnapshot | None = None


def _current_settlement(
    configuration: PlantConfiguration, cutoff: datetime
) -> QuerySet[EpiasSnapshot]:
    connection = EpiasConnection.objects.filter(
        plant_id=configuration.plant_id, enabled=True
    ).first()
    query = EpiasSnapshot.objects.filter(
        configuration=configuration,
        source_kind="settlement_injection",
        job__source_kind="settlement_injection",
        available_at__lte=cutoff,
        collected_at__lte=cutoff,
    )
    if (
        configuration.measurement_basis != "net_export"
        or connection is None
        or connection.settlement_plant_id is None
    ):
        return query.none()
    return query.filter(
        connection=connection,
        provider_plant__id=connection.settlement_plant_id,
        provider_plant__eic=connection.eic,
    )


def latest_measurement_day(
    configuration: PlantConfiguration,
    completed_before: datetime,
    cutoff: datetime,
    observation_id_cutoff: int,
) -> date | None:
    zone = ZoneInfo(configuration.binding.timezone)
    last_manual = (
        ProductionObservation.objects.filter(
            configuration=configuration,
            recorded_at__lte=cutoff,
            ends_at__lte=completed_before,
            pk__lte=observation_id_cutoff,
        )
        .order_by("-ends_at")
        .values_list("ends_at", flat=True)
        .first()
    )
    manual_day = (
        (last_manual - timedelta(microseconds=1)).astimezone(zone).date()
        if last_manual is not None
        else None
    )
    days = (
        _current_settlement(configuration, cutoff)
        .filter(
            target_day__lte=(completed_before - timedelta(microseconds=1))
            .astimezone(TIMEZONE)
            .date()
        )
        .order_by("-target_day", "-collected_at", "-available_at", "-pk")
        .distinct("target_day")
        .values_list("target_day", flat=True)
        .iterator(chunk_size=100)
    )
    seen: set[date] = set()
    for provider_day in days:
        provider_start, provider_end = local_day_bounds(provider_day, str(TIMEZONE))
        first_day = provider_start.astimezone(zone).date()
        day = (provider_end - timedelta(microseconds=1)).astimezone(zone).date()
        while day >= first_day:
            if manual_day is not None and day <= manual_day:
                return manual_day
            start, end = local_day_bounds(day, configuration.binding.timezone)
            if day not in seen and end <= completed_before:
                rows, _ = select_measurements(
                    configuration, start, end, cutoff, observation_id_cutoff=observation_id_cutoff
                )
                if rows:
                    return day
                seen.add(day)
            day -= timedelta(days=1)
    return manual_day


def select_measurements(
    configuration: PlantConfiguration,
    start: datetime,
    end: datetime,
    cutoff: datetime,
    *,
    observation_id_cutoff: int | None = None,
    settlement_snapshot_ids: list[int] | None = None,
    incident_revision_ids: list[int] | None = None,
) -> tuple[list[Measurement], list[int]]:
    manual = ProductionObservation.objects.filter(
        configuration=configuration,
        starts_at__gte=start,
        ends_at__lte=end,
        recorded_at__lte=cutoff,
    )
    manual = manual.filter(ends_at__lte=cutoff)
    if observation_id_cutoff is not None:
        manual = manual.filter(pk__lte=observation_id_cutoff)
    rows = [
        Measurement(r.starts_at, r.ends_at, r.energy_mwh, r.quality, r.source, r.pk, r.recorded_at)
        for r in manual
    ]
    if settlement_snapshot_ids is None:
        snapshots = _current_settlement(configuration, cutoff)
    else:
        snapshots = EpiasSnapshot.objects.filter(
            configuration=configuration,
            source_kind="settlement_injection",
            available_at__lte=cutoff,
            collected_at__lte=cutoff,
            pk__in=settlement_snapshot_ids,
        )
    snapshots = (
        snapshots.filter(
            target_day__gte=start.astimezone(TIMEZONE).date(),
            target_day__lte=(end - timedelta(microseconds=1)).astimezone(TIMEZONE).date(),
        )
        .order_by("target_day", "-collected_at", "-available_at", "-pk")
        .distinct("target_day")
    )
    selected = list(snapshots)
    for snapshot in selected:
        for reading in snapshot.readings:
            if not isinstance(reading, dict) or reading.get("total_mwh") is None:
                continue
            try:
                begin = datetime.fromisoformat(reading["period_start"])
                finish = datetime.fromisoformat(reading["period_end"])
                energy = float(reading["total_mwh"])
            except ValueError, TypeError, KeyError:
                continue
            if (
                begin.utcoffset() is None
                or finish.utcoffset() is None
                or finish - begin != timedelta(hours=1)
                or begin < start
                or finish > end
                or finish > cutoff
                or not isfinite(energy)
                or energy < 0
            ):
                continue
            if any(
                row.starts_at < finish and row.ends_at > begin
                for row in rows
                if row.snapshot is None
            ):
                continue
            rows.append(
                Measurement(
                    begin,
                    finish,
                    energy,
                    "valid",
                    "settlement_injection",
                    None,
                    snapshot.available_at,
                    snapshot,
                )
            )
    incidents = (
        select_incidents(configuration.plant_id, start, end, cutoff)
        if incident_revision_ids is None
        else pinned_incidents(configuration.plant_id, incident_revision_ids)
    )
    rows = [
        replace(row, quality="outage")
        if row.quality == "valid"
        and any(
            incident.starts_at < row.ends_at
            and (incident.ends_at is None or incident.ends_at > row.starts_at)
            for incident in incidents
        )
        else row
        for row in rows
    ]
    used = {row.snapshot.pk for row in rows if row.snapshot is not None}
    return sorted(rows, key=lambda r: (r.starts_at, r.pk or 0)), [
        snapshot.pk for snapshot in selected if snapshot.pk in used
    ]


def selection_fingerprint(
    configuration: PlantConfiguration,
    rows: list[Measurement],
    cutoff: datetime,
    incident_revision_ids: list[int] | None = None,
) -> tuple[str, dict[str, int | None]]:
    zone = ZoneInfo(configuration.binding.timezone)
    days = sorted({r.starts_at.astimezone(zone).date() for r in rows})
    references: dict[str, int | None] = {}
    reference_content: dict[str, Any] = {}
    for day in days:
        snapshot = select_actual_snapshot(configuration.binding, day, cutoff)
        references[day.isoformat()] = snapshot.pk if snapshot else None
        reference_content[day.isoformat()] = snapshot.normalized_intervals if snapshot else None
    values = [
        {
            "start": r.starts_at.isoformat(),
            "end": r.ends_at.isoformat(),
            "energy": r.energy_mwh,
            "quality": r.quality,
            "source": r.source,
            "manual_id": r.pk,
            "provider": r.snapshot.provider_plant if r.snapshot else None,
        }
        for r in rows
    ]
    payload = {
        "policy": MEASUREMENT_POLICY,
        "configuration": configuration.pk,
        "rows": values,
        "reference": reference_content,
        "incident_revision_ids": incident_revision_ids or [],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    return digest, references


def settlement_identity_current(run: CalibrationRun) -> bool:
    if not run.settlement_snapshot_ids:
        return True
    connection = EpiasConnection.objects.filter(plant_id=run.plant_id).first()
    if connection is None or connection.settlement_plant_id is None:
        return False
    snapshots = list(
        EpiasSnapshot.objects.filter(
            pk__in=run.settlement_snapshot_ids,
            configuration_id=run.configuration_id,
            connection=connection,
            source_kind="settlement_injection",
        )
    )
    return len(snapshots) == len(run.settlement_snapshot_ids) and all(
        snapshot.provider_plant.get("id") == connection.settlement_plant_id
        and snapshot.provider_plant.get("eic") == connection.eic
        for snapshot in snapshots
    )
