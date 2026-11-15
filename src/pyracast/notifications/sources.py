from datetime import date, datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from django.db.models import Q, QuerySet

from pyracast.calculation import calculate
from pyracast.forecasts.intraday import (
    PreparedIntradaySource,
    apply_intraday_source,
    prepare_intraday_source,
)
from pyracast.forecasts.models import ForecastSnapshot
from pyracast.forecasts.projections import saved_day_payload
from pyracast.forecasts.sources import snapshot_data
from pyracast.irradiance import local_day_bounds
from pyracast.notifications.models import NotificationDelivery


def _available_by(moment: datetime) -> Q:
    return (
        Q(collected_at__lte=moment, forecastresult__published_at__lte=moment)
        & (Q(available_at__isnull=True) | Q(available_at__lte=moment))
        & (Q(issued_at__isnull=True) | Q(issued_at__lte=moment))
    )


def eligible_saved_forecasts(
    delivery: NotificationDelivery, available_at: datetime
) -> QuerySet[ForecastSnapshot]:
    cutoff = min(available_at, delivery.preparation_deadline or delivery.scheduled_for)
    return (
        ForecastSnapshot.objects.filter(
            binding=delivery.binding, job__status="succeeded", source_kind="forecast"
        )
        .filter(_available_by(cutoff))
        .filter(
            _available_by(delivery.scheduled_for)
            | Q(job__scheduled_slot=delivery.scheduled_for, job__reference_import__isnull=True)
        )
        .select_related("forecastresult", "binding", "job")
        .order_by("-collected_at", "-available_at", "-pk")
    )


def composed_report_source(
    delivery: NotificationDelivery, day: date, available_at: datetime
) -> PreparedIntradaySource | None:
    assert delivery.binding is not None  # noqa: S101 -- Preparation pins an authorized binding.
    start, end = local_day_bounds(day, delivery.binding.timezone)
    snapshots = list(
        eligible_saved_forecasts(delivery, available_at).filter(
            coverage_start__lt=end, coverage_end__gt=start, available_at__isnull=False
        )
    )
    cutoff = min(delivery.scheduled_for, available_at)
    grace = min(available_at, delivery.preparation_deadline or delivery.scheduled_for)
    return prepare_intraday_source(
        delivery.binding,
        day,
        snapshots,
        knowledge_cutoff=cutoff,
        elapsed_cutoff=cutoff,
        actuals_grace_until=grace if grace > cutoff else None,
    )


def saved_report_results(
    delivery: NotificationDelivery, available_at: datetime
) -> list[dict[str, object] | None]:
    assert delivery.binding is not None  # noqa: S101 -- Preparation pins an authorized binding.
    today = delivery.scheduled_for.astimezone(ZoneInfo(delivery.binding.timezone)).date()
    results = [
        saved_day_payload(delivery.snapshot, today + timedelta(days=offset))
        if delivery.snapshot is not None
        else None
        for offset in (0, 1)
    ]
    try:
        source = composed_report_source(delivery, today, available_at)
        if source is not None:
            result = calculate(
                delivery.binding,
                today,
                snapshot_data(source.snapshot, intervals=list(source.composition.intervals)),
            )
            result["provenance"] = {
                "snapshot_id": source.snapshot.pk,
                "binding_id": delivery.binding_id,
                "source_kind": "forecast",
            }
            cast(dict[str, object], result["vintage"])["scope"] = "primary_forecast_source"
            apply_intraday_source(result, source)
            results[0] = result
    except ValueError, ArithmeticError, KeyError, TypeError:
        results[0] = None
    return results
