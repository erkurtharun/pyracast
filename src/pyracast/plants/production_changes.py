from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext

from pyracast.accounts.authorization import tenant_membership
from pyracast.accounts.models import WorkspaceRole
from pyracast.errors import ConflictError, ErrorCode
from pyracast.plants.events import record_plant_event
from pyracast.plants.measurements import select_measurements, select_provider_measurements
from pyracast.plants.models import (
    ObservationQuality,
    Plant,
    PlantConfiguration,
    PlantEventAction,
    ProductionObservation,
)
from pyracast.plants.observations import observation_heads
from pyracast.plants.operations import (
    ObservationImportResult,
    ObservationInput,
    _observation,
    configuration_is_current,
)
from pyracast.plants.preparation import request_preparation
from pyracast.regions.models import Region


@dataclass(frozen=True)
class ProductionChange:
    starts_at: datetime
    ends_at: datetime
    expected_revision_id: int | None
    action: str
    energy: float = 0
    quality: str = ObservationQuality.VALID
    source: str = "operator"


@dataclass(frozen=True)
class PreparedChange:
    starts_at: datetime
    ends_at: datetime
    energy_mwh: float
    quality: str
    source: str
    reverted: bool
    previous: ProductionObservation | None


def _covers(spans: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> bool:
    cursor = start
    for begin, finish in sorted(spans):
        if finish <= cursor:
            continue
        if begin > cursor:
            return False
        cursor = finish
        if cursor >= end:
            return True
    return False


def lock_production_configuration(
    tenant_id: int, region_id: int, plant_id: int, configuration_id: int, *, lock: bool = True
) -> PlantConfiguration:
    regions = Region.objects.active()
    plants = Plant.objects.all()
    if lock:
        regions = regions.select_for_update()
        plants = plants.select_for_update()
    region = regions.get(pk=region_id, tenant_id=tenant_id)
    plant = plants.get(pk=plant_id, region=region)
    configuration = PlantConfiguration.objects.select_related("binding").get(
        pk=configuration_id, plant=plant
    )
    if not configuration_is_current(configuration):
        raise ValidationError(
            gettext("Production entry requires the current approved configuration.")
        )
    return configuration


def prepare_production_changes(
    configuration: PlantConfiguration,
    unit: str,
    changes: list[ProductionChange],
    *,
    capture_revisions: bool = False,
) -> list[PreparedChange]:
    if unit not in {"MWh", "kWh"} or not 1 <= len(changes) <= 2000:
        raise ValidationError(gettext("Use MWh or kWh and a batch of 1 to 2000 observations."))
    now, zone = timezone.now(), ZoneInfo(configuration.binding.timezone)
    normalized = [
        _observation(
            ObservationInput(
                change.starts_at, change.ends_at, change.energy, change.quality, change.source
            ),
            unit,
            zone,
            now,
        )
        for change in changes
    ]
    first = min(row[0] for row in normalized).astimezone(zone).date()
    last = (max(row[1] for row in normalized) - timedelta(microseconds=1)).astimezone(zone).date()
    if (last - first).days >= 31:
        raise ValidationError(gettext("A production batch must stay within 31 local days."))
    heads = list(
        ProductionObservation.objects.filter(
            plant_id=configuration.plant_id,
            is_current=True,
            starts_at__lt=max(row[1] for row in normalized),
            ends_at__gt=min(row[0] for row in normalized),
        )
    )
    indexed = {(row.starts_at, row.ends_at): row for row in heads}
    result: list[PreparedChange] = []
    for change, (start, end, value, quality, source) in zip(changes, normalized, strict=True):
        if change.action not in {"set", "revert"}:
            raise ValidationError(gettext("Unknown production action."))
        if any(row.starts_at < end and row.ends_at > start for row in result):
            raise ValidationError(gettext("Conflicting or overlapping observation."))
        previous = indexed.get((start, end))
        if (
            not capture_revisions
            and (previous.pk if previous else None) != change.expected_revision_id
        ):
            raise ConflictError(ErrorCode.PRODUCTION_REVISION_CHANGED)
        if previous is not None and previous.configuration_id != configuration.pk:
            raise ValidationError(gettext("Production belongs to a previous configuration."))
        if change.action == "revert" and previous is None:
            raise ValidationError(gettext("There is no manual production to revert."))
        if any(
            row.pk != (previous.pk if previous else None)
            and not row.is_reverted
            and row.starts_at < end
            and row.ends_at > start
            for row in heads
        ):
            raise ValidationError(gettext("Conflicting or overlapping observation."))
        if change.action == "revert" and previous is not None:
            value, quality, source = previous.energy_mwh, previous.quality, previous.source
        result.append(
            PreparedChange(start, end, value, quality, source, change.action == "revert", previous)
        )
    return result


def change_production(
    actor: User,
    tenant_id: int,
    region_id: int,
    plant_id: int,
    *,
    configuration_id: int,
    unit: str,
    changes: list[ProductionChange],
) -> ObservationImportResult:
    with transaction.atomic():
        tenant_membership(actor, tenant_id, role=WorkspaceRole.ADMIN, lock=True)
        configuration = lock_production_configuration(
            tenant_id, region_id, plant_id, configuration_id
        )
        prepared = prepare_production_changes(configuration, unit, changes)
        ids: list[int] = []
        created = 0
        for row in prepared:
            previous = row.previous
            if previous is not None and (
                previous.energy_mwh,
                previous.quality,
                previous.source,
                previous.is_reverted,
            ) == (row.energy_mwh, row.quality, row.source, row.reverted):
                ids.append(previous.pk)
                continue
            if previous is not None:
                previous.is_current = False
                previous.save(update_fields=("is_current",))
            saved = ProductionObservation.objects.create(
                plant_id=plant_id,
                configuration=configuration,
                starts_at=row.starts_at,
                ends_at=row.ends_at,
                energy_mwh=row.energy_mwh,
                quality=row.quality,
                source=row.source,
                recorded_by=actor,
                supersedes=previous,
                is_reverted=row.reverted,
            )
            ids.append(saved.pk)
            created += 1
        if created:
            record_plant_event(
                plant_id=plant_id,
                actor=actor,
                action=PlantEventAction.PRODUCTION_CHANGED,
                details={"configuration_id": configuration_id, "ids": ids},
            )
            request_preparation([plant_id])
        return {"created": created, "existing": len(ids) - created, "count": len(ids), "ids": ids}


def production_change_warnings(
    configuration: PlantConfiguration, prepared: list[PreparedChange]
) -> list[dict[str, Any]]:
    start = min(row.starts_at for row in prepared).replace(minute=0, second=0, microsecond=0)
    last = max(row.ends_at for row in prepared)
    end = last.replace(minute=0, second=0, microsecond=0)
    if end < last:
        end += timedelta(hours=1)
    providers = select_provider_measurements(configuration, start, end, timezone.now())[0]
    replaced = {row.previous.pk for row in prepared if row.previous is not None}
    active = observation_heads(cutoff=timezone.now()).filter(
        configuration=configuration,
        is_reverted=False,
        starts_at__lt=end,
        ends_at__gt=start,
    )
    manual_spans = [(row.starts_at, row.ends_at) for row in active if row.pk not in replaced]
    manual_spans.extend((row.starts_at, row.ends_at) for row in prepared if not row.reverted)
    return [
        {"code": "provider_hour_incomplete", "starts_at": row.starts_at, "ends_at": row.ends_at}
        for row in providers
        if any(
            change.starts_at < row.ends_at and change.ends_at > row.starts_at for change in prepared
        )
        and any(begin < row.ends_at and finish > row.starts_at for begin, finish in manual_spans)
        and not _covers(manual_spans, row.starts_at, row.ends_at)
    ]


def preview_production(
    actor: User,
    tenant_id: int,
    region_id: int,
    plant_id: int,
    *,
    configuration_id: int,
    unit: str,
    observations: list[ObservationInput],
) -> dict[str, Any]:
    with transaction.atomic():
        tenant_membership(actor, tenant_id, role=WorkspaceRole.ADMIN, lock=True)
        configuration = lock_production_configuration(
            tenant_id, region_id, plant_id, configuration_id
        )
        changes = []
        for item in observations:
            changes.append(
                ProductionChange(
                    item.starts_at,
                    item.ends_at,
                    None,
                    "set",
                    item.energy,
                    item.quality,
                    item.source,
                )
            )
        prepared = prepare_production_changes(configuration, unit, changes, capture_revisions=True)
        start = min(row.starts_at for row in prepared).replace(minute=0, second=0, microsecond=0)
        last = max(row.ends_at for row in prepared)
        end = last.replace(minute=0, second=0, microsecond=0)
        if end < last:
            end += timedelta(hours=1)
        current = select_measurements(
            configuration,
            start,
            end,
            timezone.now(),
        )[0]
        current_values = {(row.starts_at, row.ends_at): row.energy_mwh for row in current}
        warnings = production_change_warnings(configuration, prepared)
        return {
            "changes": [
                {
                    "starts_at": row.starts_at,
                    "ends_at": row.ends_at,
                    "expected_revision_id": ready.previous.pk if ready.previous else None,
                    "action": row.action,
                    "energy": row.energy,
                    "quality": row.quality,
                    "source": row.source,
                }
                for row, ready in zip(changes, prepared, strict=True)
            ],
            "preview": [
                {
                    "starts_at": row.starts_at,
                    "ends_at": row.ends_at,
                    "current_energy_mwh": current_values.get((row.starts_at, row.ends_at)),
                    "new_energy_mwh": row.energy_mwh,
                    "corrected": row.previous is not None and not row.previous.is_reverted,
                }
                for row in prepared
            ],
            "warnings": warnings,
        }
