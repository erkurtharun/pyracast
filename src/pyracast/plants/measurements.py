import hashlib
import json
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

from django.db.models import F, Q, QuerySet

from pyracast.epias import TIMEZONE
from pyracast.forecasts.actuals import ACTUAL_COMPOSITION_POLICY, select_archived_actual_source
from pyracast.forecasts.history import HISTORY_SELECTION_POLICY
from pyracast.forecasts.intraday import completed_actual_intervals
from pyracast.forecasts.models import CollectionEndpoint
from pyracast.incidents.operations import pinned_incidents, select_incidents
from pyracast.irradiance import local_day_bounds
from pyracast.metering.models import EpiasCollectionJob, EpiasConnection, EpiasSnapshot
from pyracast.plants.forecast_sources import compose_snapshots
from pyracast.plants.measurement_policy import (
    LEGACY_MEASUREMENT_POLICY as LEGACY_MEASUREMENT_POLICY,
)
from pyracast.plants.measurement_policy import (
    MEASUREMENT_POLICY as MEASUREMENT_POLICY,
)
from pyracast.plants.models import (
    CalibrationRun,
    Plant,
    PlantConfiguration,
    PlantEvent,
    PlantEventAction,
    ProductionObservation,
)
from pyracast.plants.observations import observation_heads


def _source_kinds(policy: str) -> list[str]:
    if policy == LEGACY_MEASUREMENT_POLICY:
        return ["settlement_injection"]
    if policy == MEASUREMENT_POLICY:
        return ["settlement_injection", "realtime_generation"]
    raise ValueError("Unknown measurement selection policy")


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


@dataclass(frozen=True)
class MeasurementSelection:
    effective: list[Measurement]
    providers: list[Measurement]
    snapshot_ids: list[int]


@dataclass(frozen=True)
class ProductionMeasurements:
    heads: list[ProductionObservation]
    selection: MeasurementSelection


def _current_production(
    configuration: PlantConfiguration, cutoff: datetime, policy: str
) -> QuerySet[EpiasSnapshot]:
    connection = EpiasConnection.objects.filter(
        plant_id=configuration.plant_id, enabled=True
    ).first()
    query = EpiasSnapshot.objects.filter(
        configuration=configuration,
        source_kind__in=_source_kinds(policy),
        available_at__lte=cutoff,
        collected_at__lte=cutoff,
    )
    if configuration.measurement_basis != "net_export" or connection is None:
        return query.none()
    identities = Q(
        source_kind="realtime_generation", provider_plant__id=connection.provider_plant_id
    )
    if connection.settlement_plant_id is not None:
        identities |= Q(
            source_kind="settlement_injection", provider_plant__id=connection.settlement_plant_id
        )
    return query.filter(
        connection=connection,
        provider_plant__eic=connection.eic,
    ).filter(identities)


def latest_measurement_day(
    configuration: PlantConfiguration,
    completed_before: datetime,
    cutoff: datetime,
    observation_id_cutoff: int,
    *,
    policy: str = MEASUREMENT_POLICY,
) -> date | None:
    zone = ZoneInfo(configuration.binding.timezone)
    last_manual = (
        observation_heads(cutoff=cutoff, observation_id_cutoff=observation_id_cutoff)
        .filter(
            configuration=configuration,
            ends_at__lte=completed_before,
            is_reverted=False,
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
        _current_production(configuration, cutoff, policy)
        .filter(
            target_date__lte=(completed_before - timedelta(microseconds=1))
            .astimezone(TIMEZONE)
            .date()
        )
        .order_by("-target_date", "-collected_at", "-available_at", "-pk")
        .distinct("target_date")
        .values_list("target_date", flat=True)
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
                    configuration,
                    start,
                    end,
                    cutoff,
                    observation_id_cutoff=observation_id_cutoff,
                    policy=policy,
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
    production_snapshot_ids: list[int] | None = None,
    incident_revision_ids: list[int] | None = None,
    policy: str = MEASUREMENT_POLICY,
) -> tuple[list[Measurement], list[int]]:
    manual = observation_heads(cutoff=cutoff, observation_id_cutoff=observation_id_cutoff).filter(
        configuration=configuration,
        starts_at__lt=end,
        ends_at__gt=start,
        is_reverted=False,
        ends_at__lte=cutoff,
    )
    selection = _select_measurement_sources(
        configuration,
        start,
        end,
        cutoff,
        list(manual),
        production_snapshot_ids=production_snapshot_ids,
        incident_revision_ids=incident_revision_ids,
        policy=policy,
    )
    return selection.effective, selection.snapshot_ids


def select_production_measurements(
    plant: Plant,
    configuration: PlantConfiguration | None,
    start: datetime,
    end: datetime,
    cutoff: datetime,
) -> ProductionMeasurements:
    heads = list(
        observation_heads(cutoff=cutoff)
        .filter(plant=plant, starts_at__lt=end, ends_at__gt=start)
        .order_by("starts_at", "id")
    )
    if configuration is None:
        return ProductionMeasurements(heads, MeasurementSelection([], [], []))
    manual = [
        row
        for row in heads
        if row.configuration_id == configuration.pk
        and not row.is_reverted
        and row.ends_at <= cutoff
    ]
    return ProductionMeasurements(
        heads, _select_measurement_sources(configuration, start, end, cutoff, manual)
    )


def _select_measurement_sources(
    configuration: PlantConfiguration,
    start: datetime,
    end: datetime,
    cutoff: datetime,
    overlapping_manual: list[ProductionObservation],
    *,
    production_snapshot_ids: list[int] | None = None,
    incident_revision_ids: list[int] | None = None,
    policy: str = MEASUREMENT_POLICY,
) -> MeasurementSelection:
    rows = [
        Measurement(r.starts_at, r.ends_at, r.energy_mwh, r.quality, r.source, r.pk, r.created_at)
        for r in overlapping_manual
        if r.starts_at >= start and r.ends_at <= end
    ]
    providers, selected_ids = select_provider_measurements(
        configuration,
        start,
        end,
        cutoff,
        production_snapshot_ids=production_snapshot_ids,
        policy=policy,
    )
    rows.extend(
        row
        for row in providers
        if not any(
            manual.starts_at < row.ends_at and manual.ends_at > row.starts_at
            for manual in overlapping_manual
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
    return MeasurementSelection(
        sorted(rows, key=lambda r: (r.starts_at, r.pk or 0)),
        providers,
        [pk for pk in selected_ids if pk in used],
    )


def select_provider_measurements(
    configuration: PlantConfiguration,
    start: datetime,
    end: datetime,
    cutoff: datetime,
    *,
    production_snapshot_ids: list[int] | None = None,
    policy: str = MEASUREMENT_POLICY,
) -> tuple[list[Measurement], list[int]]:
    kinds = _source_kinds(policy)
    if production_snapshot_ids is None:
        snapshots = _current_production(configuration, cutoff, policy)
    else:
        snapshots = EpiasSnapshot.objects.filter(
            configuration=configuration,
            source_kind__in=kinds,
            available_at__lte=cutoff,
            collected_at__lte=cutoff,
            pk__in=production_snapshot_ids,
        )
    snapshots = (
        snapshots.filter(
            target_date__gte=start.astimezone(TIMEZONE).date(),
            target_date__lte=(end - timedelta(microseconds=1)).astimezone(TIMEZONE).date(),
        )
        .order_by("target_date", "source_kind", "-collected_at", "-available_at", "-pk")
        .distinct("target_date", "source_kind")
    )
    selected = sorted(
        snapshots.select_related("job").defer("raw_payload"),
        key=lambda item: kinds.index(item.source_kind),
    )
    provider_rows: dict[datetime, Measurement] = {}
    for snapshot in selected:
        if (
            snapshot.job.source_kind != snapshot.source_kind
            or snapshot.job.configuration_id != configuration.pk
            or snapshot.normalization_policy != "hour-start-istanbul-v1"
        ):
            continue
        for row in _provider_measurements(snapshot, start, end, cutoff):
            if row.starts_at in provider_rows:
                continue
            provider_rows[row.starts_at] = row
    used = {row.snapshot.pk for row in provider_rows.values() if row.snapshot is not None}
    return sorted(provider_rows.values(), key=lambda r: r.starts_at), [
        snapshot.pk
        for snapshot in sorted(selected, key=lambda s: (s.target_date, s.pk))
        if snapshot.pk in used
    ]


def _provider_measurements(
    snapshot: EpiasSnapshot, start: datetime, end: datetime, cutoff: datetime
) -> list[Measurement]:
    rows = []
    for reading in snapshot.readings:
        if (
            not isinstance(reading, dict)
            or reading.get("total_mwh") is None
            or reading.get("reported") is False
        ):
            continue
        try:
            begin = datetime.fromisoformat(reading["period_start"])
            finish = datetime.fromisoformat(reading["period_end"])
            energy = float(reading["total_mwh"])
        except ValueError, TypeError, KeyError:
            continue
        local_begin = begin.astimezone(TIMEZONE) if begin.utcoffset() is not None else begin
        if (
            begin.utcoffset() is None
            or finish.utcoffset() is None
            or finish - begin != timedelta(hours=1)
            or local_begin.date() != snapshot.target_date
            or any((local_begin.minute, local_begin.second, local_begin.microsecond))
            or begin < start
            or finish > end
            or finish > cutoff
            or not isfinite(energy)
            or energy < 0
        ):
            continue
        rows.append(
            Measurement(
                begin,
                finish,
                energy,
                "valid",
                snapshot.source_kind,
                None,
                snapshot.available_at,
                snapshot,
            )
        )
    return rows


def selection_fingerprint(
    configuration: PlantConfiguration,
    rows: list[Measurement],
    cutoff: datetime,
    incident_revision_ids: list[int] | None = None,
    *,
    policy: str = MEASUREMENT_POLICY,
) -> tuple[str, dict[str, object]]:
    zone = ZoneInfo(configuration.binding.timezone)
    days = sorted({r.starts_at.astimezone(zone).date() for r in rows})
    references: dict[str, object] = {}
    reference_content: dict[str, Any] = {}
    for day in days:
        source = select_archived_actual_source(configuration.binding, day, cutoff)
        snapshot = source.snapshot if source is not None else None
        references[day.isoformat()] = source.pin if source is not None else None
        reference_content[day.isoformat()] = (
            {
                "intervals": snapshot.normalized_intervals,
                "endpoint": snapshot.job.endpoint,
                "historical_import": snapshot.job.reference_import_id is not None,
            }
            if snapshot
            else None
        )
        if (
            source is not None
            and snapshot is not None
            and snapshot.job.endpoint == CollectionEndpoint.ESTIMATED_ACTUALS
            and snapshot.job.reference_import_id is None
        ):
            start, end = local_day_bounds(day, configuration.binding.timezone)
            composition = source.composition or compose_snapshots([snapshot], start, end)
            reference_content[day.isoformat()] = {
                "intervals": [
                    {
                        key: value
                        for key, value in row.items()
                        if key not in {"source_snapshot_id", "source_kind"}
                    }
                    for row in completed_actual_intervals(composition.intervals, cutoff)
                ],
                "endpoint": "estimated_actuals",
                "historical_import": False,
                "selection_policy": ACTUAL_COMPOSITION_POLICY,
            }
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
        "policy": policy,
        "reference_selection_policy": HISTORY_SELECTION_POLICY,
        "native_composition_policy": ACTUAL_COMPOSITION_POLICY,
        "configuration": configuration.pk,
        "rows": values,
        "reference": reference_content,
        "incident_revision_ids": incident_revision_ids or [],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    return digest, references


def production_identity_current(run: CalibrationRun) -> bool:
    if not run.production_snapshot_ids:
        return True
    connection = EpiasConnection.objects.filter(plant_id=run.plant_id).first()
    if connection is None:
        return False
    snapshots = list(
        EpiasSnapshot.objects.filter(
            pk__in=run.production_snapshot_ids,
            configuration_id=run.configuration_id,
            connection=connection,
            source_kind__in=_source_kinds(run.measurement_selection_policy),
        ).select_related("job")
    )
    return len(snapshots) == len(run.production_snapshot_ids) and all(
        snapshot.provider_plant.get("id") is not None
        and snapshot.provider_plant.get("id")
        == (
            connection.settlement_plant_id
            if snapshot.source_kind == "settlement_injection"
            else connection.provider_plant_id
        )
        and snapshot.provider_plant.get("eic") == connection.eic
        and snapshot.job.source_kind == snapshot.source_kind
        and snapshot.job.configuration_id == run.configuration_id
        and snapshot.normalization_policy == "hour-start-istanbul-v1"
        for snapshot in snapshots
    )


def production_identity_at(run: CalibrationRun, as_of: datetime) -> bool:
    if not run.production_snapshot_ids:
        return True
    event = (
        PlantEvent.objects.filter(
            plant_id=run.plant_id, action=PlantEventAction.EPIAS_CONNECTED, created_at__lte=as_of
        )
        .order_by("-created_at", "-pk")
        .first()
    )
    if event is None:
        return False
    identity = event.details
    revision = _provider_identifier(identity.get("revision"))
    if (
        revision is None
        or _provider_identifier(identity.get("provider_plant_id")) is None
        or not isinstance(identity.get("eic"), str)
        or not identity["eic"]
    ):
        return False
    snapshots = list(
        EpiasSnapshot.objects.filter(
            pk__in=run.production_snapshot_ids,
            configuration_id=run.configuration_id,
            configuration__plant_id=run.plant_id,
            connection__plant_id=run.plant_id,
            job__connection_id=F("connection_id"),
            job__configuration_id=run.configuration_id,
            job__plant_id=run.plant_id,
            job__created_at__lte=as_of,
            source_kind__in=_source_kinds(run.measurement_selection_policy),
            collected_at__lte=as_of,
            available_at__lte=as_of,
        ).select_related("job")
    )
    if len(snapshots) != len(run.production_snapshot_ids):
        return False
    settlement_providers = (
        list(
            EpiasCollectionJob.objects.filter(
                connection__plant_id=run.plant_id,
                plant_id=run.plant_id,
                connection_revision=revision,
                source_kind="settlement_injection",
                created_at__lte=as_of,
            ).values_list("provider_plant", flat=True)
        )
        if any(snapshot.source_kind == "settlement_injection" for snapshot in snapshots)
        else []
    )
    return all(
        _historical_provider_matches(snapshot, identity, settlement_providers)
        for snapshot in snapshots
    )


def _historical_provider_matches(
    snapshot: EpiasSnapshot,
    identity: dict[str, Any],
    settlement_providers: list[dict[str, Any]],
) -> bool:
    provider = snapshot.provider_plant
    if (
        _provider_identifier(provider.get("id")) is None
        or provider.get("eic") != identity.get("eic")
        or snapshot.job.source_kind != snapshot.source_kind
        or snapshot.normalization_policy != "hour-start-istanbul-v1"
    ):
        return False
    if snapshot.source_kind == "realtime_generation":
        return bool(provider.get("id") == identity.get("provider_plant_id"))
    # Settlement IDs occupy a separate namespace and are absent from connection events.
    return bool(settlement_providers) and all(
        _provider_identifier(row.get("id")) == provider.get("id")
        and row.get("eic") == provider.get("eic")
        for row in settlement_providers
    )


def _provider_identifier(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
