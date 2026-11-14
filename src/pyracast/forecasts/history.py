from datetime import date, datetime, timedelta
from typing import Literal, cast
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db.models import Case, IntegerField, Q, Value, When

from pyracast.calculation import calculate
from pyracast.connections.contracts import ProviderData
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.capacity_normalization import normalization_provenance
from pyracast.forecasts.coverage import history_day_complete
from pyracast.forecasts.jobs import binding_is_current
from pyracast.forecasts.models import ForecastResult, ForecastSnapshot
from pyracast.forecasts.sources import current_forecast_snapshots, snapshot_data
from pyracast.irradiance import local_day_bounds
from pyracast.plants.forecast_sources import apply_composition, compose_snapshots
from pyracast.regions.models import Region

HISTORY_SELECTION_POLICY = "native-then-authorized-scale-complete-actuals-v4"


class OutlookRequestError(Exception):
    def __init__(self, code: Literal["date_out_of_range", "completed_day_required"]) -> None:
        self.code = code
        super().__init__(code)


def select_actual_snapshot(
    binding: SiteBinding, target_date: date, as_of: datetime
) -> ForecastSnapshot | None:
    start, end = local_day_bounds(target_date, binding.timezone)
    candidates = (
        ForecastSnapshot.objects.select_related("job__binding__provider_account")
        .filter(
            binding=binding,
            job__status="succeeded",
            source_kind="estimated_actuals",
            coverage_start__lt=end,
            coverage_end__gt=start,
            collected_at__gte=end,
            collected_at__lte=as_of,
            available_at__lte=as_of,
        )
        .filter(Q(issued_at__isnull=True) | Q(issued_at__lte=as_of))
        .alias(
            source_priority=Case(
                When(
                    job__endpoint="estimated_actuals",
                    job__reference_import__isnull=True,
                    then=Value(0),
                ),
                When(
                    job__endpoint="scaled_estimated_actuals",
                    capacity_normalization__source_snapshot__job__endpoint="estimated_actuals",
                    then=Value(1),
                ),
                default=Value(2),
                output_field=IntegerField(),
            )
        )
        .order_by("source_priority", "-collected_at", "-available_at", "-pk")
    )
    latest = None
    for snapshot in candidates.iterator():
        if latest is None:
            latest = snapshot
        if history_day_complete(snapshot, target_date, binding.timezone):
            return snapshot
    return latest


def historical_irradiance(snapshot: ForecastSnapshot, target_date: date) -> dict[str, object]:
    result = calculate(
        snapshot.job.binding,
        target_date,
        ProviderData(
            raw_payload=snapshot.raw_payload,
            intervals=snapshot.normalized_intervals,
            issued_at=snapshot.issued_at,
            collected_at=snapshot.collected_at,
            source_kind="estimated_actuals",
        ),
    )
    result["reference_capacity_normalization"] = normalization_provenance(snapshot)
    return result


def regional_history(region: Region, target_date: date, as_of: datetime) -> dict[str, object]:
    binding = region.approved_binding
    snapshot = select_actual_snapshot(binding, target_date, as_of) if binding else None
    result = historical_irradiance(snapshot, target_date) if snapshot else None
    return {
        "region_id": region.pk,
        "date": target_date,
        "timezone": binding.timezone if binding else region.timezone,
        "status": region.status,
        "source_kind": "estimated_actuals",
        "freshness": "missing"
        if snapshot is None
        else "fresh"
        if binding is not None and binding_is_current(binding)
        else "stale",
        "unavailable_reason": "estimated_actuals_unavailable" if snapshot is None else None,
        "result": result,
        "collected_at": snapshot.collected_at if snapshot else None,
        "issued_at": snapshot.issued_at if snapshot else None,
        "binding_revision": snapshot.job.binding_id if snapshot else None,
        "scheduled_slot": snapshot.job.scheduled_slot if snapshot else None,
        "late": snapshot.collected_at > snapshot.job.scheduled_slot if snapshot else None,
    }


def regional_outlook(
    region: Region,
    target_date: date,
    source: Literal["auto", "forecast", "estimated_actuals"],
    now: datetime,
) -> dict[str, object]:
    today = now.astimezone(ZoneInfo(region.timezone)).date()
    if not today - timedelta(days=366) <= target_date <= today + timedelta(days=14):
        raise OutlookRequestError("date_out_of_range")
    if source == "estimated_actuals" and target_date >= today:
        raise OutlookRequestError("completed_day_required")
    if source == "estimated_actuals" or (source == "auto" and target_date < today):
        return regional_history(region, target_date, now)
    if target_date >= today:
        return _regional_current_forecast(region, target_date, now)
    saved = (
        ForecastResult.objects.select_related("snapshot__job__binding__provider_account")
        .filter(
            snapshot__job__binding__region=region,
            snapshot__job__target_date=target_date,
            snapshot__source_kind="forecast",
            snapshot__collected_at__lte=now,
            published_at__lte=now,
        )
        .filter(Q(snapshot__issued_at__isnull=True) | Q(snapshot__issued_at__lte=now))
        .order_by("-snapshot__collected_at", "-published_at", "-pk")
        .first()
    )
    fresh = (
        saved is not None
        and binding_is_current(saved.snapshot.job.binding)
        and saved.snapshot.collected_at >= now - timedelta(hours=24)
    )
    return {
        "region_id": region.pk,
        "date": target_date,
        "timezone": saved.snapshot.job.binding.timezone if saved else region.timezone,
        "status": region.status,
        "source_kind": "forecast",
        "unavailable_reason": "forecast_unavailable" if saved is None else None,
        "freshness": "missing" if saved is None else "fresh" if fresh else "stale",
        "result": saved.payload if saved else None,
        "collected_at": saved.snapshot.collected_at if saved else None,
        "issued_at": saved.snapshot.issued_at if saved else None,
        "binding_revision": saved.snapshot.job.binding_id if saved else None,
        "scheduled_slot": saved.snapshot.job.scheduled_slot if saved else None,
        "late": saved.snapshot.collected_at > saved.snapshot.job.scheduled_slot if saved else None,
    }


def _regional_current_forecast(
    region: Region, target_date: date, now: datetime
) -> dict[str, object]:
    binding = region.approved_binding
    start, end = local_day_bounds(target_date, binding.timezone if binding else region.timezone)
    snapshots: list[ForecastSnapshot] = []
    if binding is not None:
        snapshots = current_forecast_snapshots(
            binding_id=binding.pk, start=start, end=end, as_of=now
        )
    snapshot = snapshots[0] if snapshots else None
    result = None
    used_snapshots: list[ForecastSnapshot] = []
    if snapshot is not None and binding is not None:
        try:
            composition = compose_snapshots(snapshots, start, end)
            result = calculate(
                binding,
                target_date,
                snapshot_data(snapshot, intervals=list(composition.intervals)),
            )
            result["reference_capacity_normalization"] = normalization_provenance(snapshot)
            result["provenance"] = {
                "snapshot_id": snapshot.pk,
                "binding_id": binding.pk,
                "source_kind": "forecast",
            }
            cast(dict[str, object], result["vintage"])["scope"] = "primary_forecast_source"
            apply_composition(result, snapshots, composition)
            used_snapshots = [
                source for source in snapshots if source.pk in composition.snapshot_ids
            ]
        except (ValueError, ArithmeticError) as exc:
            raise ValidationError("calculation_out_of_range") from exc
    fresh = (
        binding is not None
        and binding_is_current(binding)
        and all(source.collected_at >= now - timedelta(hours=24) for source in used_snapshots)
    )
    return {
        "region_id": region.pk,
        "date": target_date,
        "timezone": binding.timezone if binding else region.timezone,
        "status": region.status,
        "source_kind": "forecast",
        "unavailable_reason": "forecast_unavailable" if snapshot is None else None,
        "freshness": "missing" if snapshot is None else "fresh" if fresh else "stale",
        "result": result,
        "collected_at": snapshot.collected_at if snapshot else None,
        "issued_at": snapshot.issued_at if snapshot else None,
        "binding_revision": snapshot.job.binding_id if snapshot else None,
        "scheduled_slot": snapshot.job.scheduled_slot if snapshot else None,
        "late": snapshot.collected_at > snapshot.job.scheduled_slot if snapshot else None,
    }
