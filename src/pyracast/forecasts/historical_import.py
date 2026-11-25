from collections import defaultdict
from datetime import date, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from pyracast.connections.models import SiteBinding
from pyracast.forecasts.models import (
    CollectionEndpoint,
    CollectionJob,
    CollectionJobStatus,
    ForecastSnapshot,
    HistoricalReferenceImport,
    SnapshotSourceKind,
)
from pyracast.forecasts.trust import problem_messages, reference_problems
from pyracast.irradiance.solcast_export import PowerFile
from pyracast.plants.models import Plant
from pyracast.regions.audit import record_audit
from pyracast.regions.models import AuditAction, Region

BATCH = 500


def day_indexes(power: PowerFile, zone: ZoneInfo) -> dict[date, list[int]]:
    """Interval indexes per local day; an interval crossing midnight belongs to both days."""
    days: dict[date, list[int]] = defaultdict(list)
    for index, interval in enumerate(power.intervals):
        day = interval.start.astimezone(zone).date()
        last = (interval.end - timedelta(microseconds=1)).astimezone(zone).date()
        while day <= last:
            days[day].append(index)
            day += timedelta(days=1)
    return days


def require_reference(binding: SiteBinding) -> None:
    problems = reference_problems(binding)
    if problems:
        raise ValidationError(problem_messages(problems))


def _confirm_under_lock(
    region_id: int,
    binding: SiteBinding,
    expected_configuration_ids: tuple[tuple[int, int | None], ...] | None,
) -> Region:
    region = Region.objects.active().select_for_update().get(pk=region_id)
    current = SiteBinding.objects.select_related("provider_account").get(pk=binding.pk)
    current.region = region
    require_reference(current)
    if expected_configuration_ids is not None and expected_configuration_ids != tuple(
        Plant.objects.select_for_update()
        .filter(region=region)
        .order_by("pk")
        .values_list("pk", "current_configuration_id")
    ):
        raise ValidationError(_("The plant's technical details changed. Preview the file again."))
    return region


def _snapshots(
    record: HistoricalReferenceImport, binding: SiteBinding, power: PowerFile
) -> list[ForecastSnapshot]:
    moment = record.created_at
    snapshots = []
    for day, indexes in day_indexes(power, ZoneInfo(binding.timezone)).items():
        intervals = [power.intervals[index] for index in indexes]
        snapshots.append(
            ForecastSnapshot(
                job=CollectionJob(
                    binding=binding,
                    reference_import=record,
                    endpoint=CollectionEndpoint.HISTORICAL_IMPORT,
                    scheduled_for=moment,
                    target_date=day,
                    status=CollectionJobStatus.SUCCEEDED,
                    available_at=moment,
                ),
                binding=binding,
                raw_payload={
                    "estimated_actuals": [power.raw_rows[index] for index in indexes],
                    "historical_import": {"id": record.pk, "sha256": power.sha256},
                },
                normalized_intervals=[
                    {
                        "start": interval.start.isoformat(),
                        "end": interval.end.isoformat(),
                        "power_kw": interval.power_kw,
                    }
                    for interval in intervals
                ],
                collected_at=moment,
                available_at=moment,
                issued_at=None,
                source_kind=SnapshotSourceKind.ESTIMATED_ACTUALS,
                coverage_start=intervals[0].start,
                coverage_end=intervals[-1].end,
            )
        )
    return snapshots


def archive_reference(
    actor: User,
    binding: SiteBinding,
    power: PowerFile,
    source_name: str,
    manifest: dict[str, Any],
    *,
    expected_configuration_ids: tuple[tuple[int, int | None], ...] | None = None,
) -> tuple[HistoricalReferenceImport, bool]:
    """Archives one file per binding and digest. The rows are written before the region lock is
    taken, so collection and reads of the plant and its provider account wait only for the
    final check and commit; a repeated file returns the first import."""
    try:
        with transaction.atomic():
            record = HistoricalReferenceImport.objects.create(
                binding=binding,
                content_sha256=power.sha256,
                source_name=source_name,
                source_settings=manifest,
                imported_by=actor,
                created_at=timezone.now(),
                interval_count=len(power.intervals),
            )
            snapshots = _snapshots(record, binding, power)
            CollectionJob.objects.bulk_create(
                [snapshot.job for snapshot in snapshots], batch_size=BATCH
            )
            ForecastSnapshot.objects.bulk_create(snapshots, batch_size=BATCH)
            region = _confirm_under_lock(binding.region_id, binding, expected_configuration_ids)
            record_audit(
                actor=actor,
                region=region,
                action=AuditAction.HISTORICAL_REFERENCE_IMPORTED,
                revision=region.revision,
            )
            from pyracast.plants.preparation import request_region_preparation

            request_region_preparation(region.pk)
    except IntegrityError:
        existing = HistoricalReferenceImport.objects.filter(
            binding=binding, content_sha256=power.sha256
        ).first()
        if existing is None:
            raise
        return existing, False
    return record, True
