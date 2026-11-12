from datetime import date, datetime

from django.db.models import Case, IntegerField, Q, Value, When

from pyracast.calculation import calculate
from pyracast.connections.contracts import ProviderData
from pyracast.connections.models import SiteBinding
from pyracast.forecasts.capacity_normalization import normalization_provenance
from pyracast.forecasts.coverage import history_day_complete
from pyracast.forecasts.jobs import binding_is_current
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.irradiance import local_day_bounds
from pyracast.regions.models import Region

HISTORY_SELECTION_POLICY = "native-then-authorized-scale-complete-actuals-v4"


def select_actual_snapshot(
    binding: SiteBinding, target_date: date, as_of: datetime
) -> ForecastSnapshot | None:
    start, end = local_day_bounds(target_date, binding.timezone)
    candidates = (
        ForecastSnapshot.objects.select_related("job__binding__provider_account")
        .filter(
            job__binding=binding,
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
