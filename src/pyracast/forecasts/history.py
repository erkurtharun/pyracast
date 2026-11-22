from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import Literal, cast

from django.db.models import F, Q

from pyracast.calculation import calculate
from pyracast.connections.contracts import ProviderData
from pyracast.connections.currency import binding_is_current
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.actuals import (
    ACTUAL_COMPOSITION_POLICY,
    archived_actual_irradiance,
    first_complete_actuals,
    select_archived_actual_source,
    single_snapshot_actual_queryset,
)
from pyracast.forecasts.capacity_normalization import normalization_provenance
from pyracast.forecasts.composition import ForecastComposition
from pyracast.forecasts.coverage import history_day_complete
from pyracast.forecasts.faults import FROZEN_PAYLOAD_FAULTS, stored_data
from pyracast.forecasts.intraday import (
    VINTAGE_SCOPE,
    ActualsMode,
    PreparedIntradaySource,
    apply_intraday_source,
    prepare_intraday_source,
)
from pyracast.forecasts.models import (
    CollectionJobStatus,
    ForecastResult,
    ForecastSnapshot,
    SnapshotSourceKind,
)
from pyracast.forecasts.projections import saved_day_payload
from pyracast.forecasts.sources import (
    current_forecast_snapshots,
    mark_forecast_fallback,
    snapshot_data,
)
from pyracast.intervals import NormalizedInterval
from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecast_sources import apply_composition, compose_snapshots
from pyracast.regions.models import Region

HISTORY_SELECTION_POLICY = "native-then-authorized-scale-complete-actuals-v4"
COLLECTION_OVERDUE_AFTER = timedelta(hours=24)

type ResultState = tuple[Literal["fresh", "stale", "archived"], str | None]
ARCHIVED: ResultState = ("archived", None)


def live_state(
    *, connection_current: bool, oldest_collected_at: datetime | None, now: datetime
) -> ResultState:
    """A result of a day still running: stale behind an inactive connection or an overdue
    collection."""
    if not connection_current:
        return "stale", "connection_inactive"
    if oldest_collected_at is not None and oldest_collected_at < now - COLLECTION_OVERDUE_AFTER:
        return "stale", "collection_overdue"
    return "fresh", None


def result_state(available: bool, state: ResultState) -> dict[str, str | None]:
    freshness, stale_reason = state if available else ("missing", None)
    return {"freshness": freshness, "stale_reason": stale_reason}


def regional_envelope(
    target_date: date,
    zone: str,
    source_kind: str,
    snapshot: ForecastSnapshot | None,
    result: dict[str, object] | None,
    *,
    state: ResultState,
    unavailable_reason: str,
) -> dict[str, object]:
    return {
        "date": target_date,
        "timezone": zone,
        "source_kind": source_kind,
        **result_state(snapshot is not None, state),
        "unavailable_reason": unavailable_reason if snapshot is None else None,
        "result": result,
        "collected_at": snapshot.collected_at if snapshot else None,
        "issued_at": snapshot.issued_at if snapshot else None,
        "binding_id": snapshot.binding_id if snapshot else None,
    }


def select_actual_snapshot(
    binding: SiteBinding, target_date: date, as_of: datetime
) -> ForecastSnapshot | None:
    start, end = local_day_bounds(target_date, binding.timezone)
    return first_complete_actuals(
        single_snapshot_actual_queryset(
            binding, start, end, collected_from=end, as_of=as_of
        ).iterator(),
        target_date,
        binding.timezone,
    )


def historical_irradiance(snapshot: ForecastSnapshot, target_date: date) -> dict[str, object]:
    result = calculate(
        snapshot.job.binding,
        target_date,
        ProviderData(
            raw_payload=snapshot.raw_payload,
            intervals=NormalizedInterval.from_rows(snapshot.normalized_intervals),
            issued_at=snapshot.issued_at,
            collected_at=snapshot.collected_at,
            source_kind="estimated_actuals",
        ),
    )
    result["reference_capacity_normalization"] = normalization_provenance(snapshot)
    return result


def actuals_ready(
    snapshot: ForecastSnapshot | None, target_date: date, payload: dict[str, object]
) -> bool:
    daylight = cast(dict[str, object], payload["daylight"])
    if daylight["status"] == "polar_night":
        provenance = cast(dict[str, object], payload.get("provenance", {}))
        if provenance.get("selection_policy") == ACTUAL_COMPOSITION_POLICY:
            return provenance["input_day_complete"] is True
        return snapshot is not None and history_day_complete(
            snapshot, target_date, snapshot.binding.timezone
        )
    return daylight["input_coverage_fraction"] == 1


def regional_history(
    region: Region, target_date: date, as_of: datetime, *, fallback_forecast: bool = False
) -> dict[str, object]:
    binding = region.approved_binding
    with stored_data("region", region.pk, target_date, "estimated_actuals"):
        source = select_archived_actual_source(binding, target_date, as_of) if binding else None
        snapshot = source.snapshot if source else None
        result = (
            archived_actual_irradiance(binding, target_date, source)
            if source is not None and source.composition is not None and binding is not None
            else historical_irradiance(snapshot, target_date)
            if snapshot is not None
            else None
        )
        ready = result is not None and actuals_ready(snapshot, target_date, result)
    if fallback_forecast and not ready:
        fallback = _regional_saved_forecast(region, target_date, as_of)
        if fallback["result"] is not None:
            return fallback
    return regional_envelope(
        target_date,
        binding.timezone if binding else region.timezone,
        "estimated_actuals",
        snapshot,
        result,
        state=ARCHIVED,
        unavailable_reason="estimated_actuals_unavailable",
    )


def saved_region_forecast(region: Region, target_date: date, now: datetime) -> dict[str, object]:
    """The forecast published before the day's first daylight, as the plant archive selects
    its issuances; the payload is the one saved at publication."""
    from pyracast.plants.forecast_archive import ARCHIVE_SELECTION_POLICY, forecast_cutoff

    cutoffs = {
        binding.pk: forecast_cutoff(target_date, binding)
        for binding in SiteBinding.objects.filter(region=region)
    }
    zone = region.approved_binding.timezone if region.approved_binding else region.timezone
    start, end = local_day_bounds(target_date, zone)
    eligible = Q(pk__in=[])
    for binding_id, cutoff in cutoffs.items():
        eligible |= Q(
            snapshot__binding_id=binding_id,
            snapshot__collected_at__lt=cutoff,
            published_at__lt=cutoff,
        ) & (
            Q(snapshot__issued_at__isnull=True)
            | Q(
                snapshot__issued_at__lt=cutoff,
                snapshot__issued_at__lte=F("snapshot__collected_at"),
            )
        )
    saved = (
        ForecastResult.objects.select_related("snapshot__binding")
        .defer("snapshot__raw_payload")
        .filter(
            eligible,
            snapshot__source_kind=SnapshotSourceKind.FORECAST,
            snapshot__job__status=CollectionJobStatus.SUCCEEDED,
            snapshot__coverage_start__lt=end,
            snapshot__coverage_end__gt=start,
            published_at__lte=now,
        )
        .order_by("-snapshot__collected_at", "-pk")
        .first()
    )
    snapshot = saved.snapshot if saved is not None else None
    with stored_data("region", region.pk, target_date, "archived_forecast"):
        payload = saved_day_payload(snapshot, target_date) if snapshot is not None else None
    result = None
    if saved is not None and snapshot is not None and payload is not None:
        result = deepcopy(payload)
        result["provenance"] = {
            "selection_policy": ARCHIVE_SELECTION_POLICY,
            "snapshot_id": snapshot.pk,
            "collected_at": snapshot.collected_at.isoformat(),
            "published_at": saved.published_at.isoformat(),
            "selection_cutoff": cutoffs[snapshot.binding_id].isoformat(),
        }
    return regional_envelope(
        target_date,
        snapshot.binding.timezone if snapshot is not None else zone,
        "forecast",
        snapshot if result is not None else None,
        result,
        state=ARCHIVED,
        unavailable_reason="forecast_unavailable",
    )


def _regional_saved_forecast(region: Region, target_date: date, now: datetime) -> dict[str, object]:
    start, end = local_day_bounds(target_date, region.timezone)
    cutoff = min(now, end - timedelta(microseconds=1))
    candidates = (
        ForecastSnapshot.objects.select_related("binding__provider_account")
        .filter(
            binding__region=region,
            job__status=CollectionJobStatus.SUCCEEDED,
            source_kind=SnapshotSourceKind.FORECAST,
            binding__created_at__lte=cutoff,
            coverage_start__lt=end,
            coverage_end__gt=start,
            collected_at__lte=cutoff,
            available_at__lte=cutoff,
            forecastresult__published_at__lte=cutoff,
        )
        .filter(Q(issued_at__isnull=True) | Q(issued_at__lte=cutoff))
        .order_by("-collected_at", "-available_at", "-pk")
    )
    latest = candidates.first()
    binding = latest.binding if latest else None
    while binding is not None:
        _, configured_end = local_day_bounds(target_date, binding.timezone)
        narrowed = min(cutoff, configured_end - timedelta(microseconds=1))
        if narrowed == cutoff:
            break
        cutoff = narrowed
        latest = (
            candidates.filter(
                binding__created_at__lte=cutoff,
                collected_at__lte=cutoff,
                available_at__lte=cutoff,
                forecastresult__published_at__lte=cutoff,
            )
            .filter(Q(issued_at__isnull=True) | Q(issued_at__lte=cutoff))
            .first()
        )
        binding = latest.binding if latest else None
    outlook = regional_composed_forecast(region, target_date, cutoff, now, binding)
    payload = cast(dict[str, object] | None, outlook["result"])
    if payload is not None:
        mark_forecast_fallback(payload, cutoff)
    return outlook


def composed_irradiance(
    binding: SiteBinding,
    target_date: date,
    *,
    snapshot: ForecastSnapshot,
    source_kind: str,
    snapshots: list[ForecastSnapshot],
    composition: ForecastComposition,
    source: PreparedIntradaySource | None,
    normalization: bool = False,
) -> dict[str, object]:
    result = calculate(
        binding,
        target_date,
        snapshot_data(snapshot, intervals=list(composition.intervals)),
    )
    if normalization:
        result["reference_capacity_normalization"] = normalization_provenance(snapshot)
    result["provenance"] = {
        "snapshot_id": snapshot.pk,
        "binding_id": binding.pk,
        "source_kind": source_kind,
    }
    cast(dict[str, object], result["vintage"])["scope"] = VINTAGE_SCOPE
    if source is not None:
        apply_intraday_source(result, source)
    else:
        apply_composition(result, snapshots, composition)
    return result


def regional_composed_forecast(
    region: Region,
    target_date: date,
    cutoff: datetime,
    now: datetime,
    binding: SiteBinding | None,
    *,
    actuals: ActualsMode = "none",
) -> dict[str, object]:
    actuals_only = actuals == "only"
    start, end = local_day_bounds(target_date, binding.timezone if binding else region.timezone)
    snapshots: list[ForecastSnapshot] = []
    if binding is not None:
        snapshots = current_forecast_snapshots(
            binding_id=binding.pk, start=start, end=end, as_of=cutoff
        )
    with stored_data("region", region.pk, target_date, "actuals", FROZEN_PAYLOAD_FAULTS):
        source = (
            prepare_intraday_source(
                binding,
                target_date,
                snapshots,
                knowledge_cutoff=cutoff,
                elapsed_cutoff=cutoff,
                actuals_only=actuals_only,
            )
            if binding is not None and actuals != "none"
            else None
        )
    source_kind = "estimated_actuals" if actuals_only else "forecast"
    if source is not None:
        snapshots = source.snapshots
        source_kind = source.source_kind
    elif actuals_only:
        snapshots = []
    snapshot = snapshots[0] if snapshots else None
    result = None
    used_snapshots: list[ForecastSnapshot] = []
    if snapshot is not None and binding is not None:
        with stored_data("region", region.pk, target_date, "irradiance"):
            composition = (
                source.composition
                if source is not None
                else compose_snapshots(snapshots, start, end)
            )
            result = composed_irradiance(
                binding,
                target_date,
                snapshot=snapshot,
                source_kind=source_kind,
                snapshots=snapshots,
                composition=composition,
                source=source,
                normalization=True,
            )
            used_snapshots = [row for row in snapshots if row.pk in composition.snapshot_ids]
    return regional_envelope(
        target_date,
        binding.timezone if binding else region.timezone,
        source_kind,
        snapshot,
        result,
        state=ARCHIVED
        if end <= now
        else live_state(
            connection_current=binding is not None and binding_is_current(binding),
            oldest_collected_at=min((row.collected_at for row in used_snapshots), default=None),
            now=now,
        ),
        unavailable_reason="estimated_actuals_unavailable"
        if actuals_only
        else "forecast_unavailable",
    )
