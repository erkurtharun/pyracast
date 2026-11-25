from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from math import isfinite
from typing import Any, Literal, TypedDict, cast
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext, gettext_lazy

from pyracast.accounts.authorization import tenant_membership
from pyracast.accounts.models import WorkspaceRole
from pyracast.db import read_snapshot
from pyracast.errors import UNEXPECTED, ConflictError, DomainError, ErrorCode
from pyracast.plants.availability import interval_is_covered, reference_intervals
from pyracast.plants.events import record_plant_event
from pyracast.plants.forecasting import power_limit
from pyracast.plants.measurements import (
    Measurement,
    select_measurements,
    select_provider_measurements,
)
from pyracast.plants.models import (
    ObservationQuality,
    Plant,
    PlantConfiguration,
    PlantEventAction,
    ProductionObservation,
)
from pyracast.plants.numerics import PowerInterval
from pyracast.plants.observations import observation_heads
from pyracast.plants.operations import configuration_is_current
from pyracast.plants.preparation import request_preparation
from pyracast.regions.models import Region

PRODUCTION_BATCH_LIMIT = 2000
ROW_TIME_INVALID = gettext_lazy("Row %(row)s: the date or time is not valid.")
OVERLAPPING = gettext_lazy(
    "Some values overlap in time with each other or with saved values. "
    "Enter one value per interval."
)
DETAILS_CHANGED = gettext_lazy(
    "The plant's technical details changed after these values were entered, "
    "so they cannot be saved."
)
# Mean power above this multiple of the plant's power ceiling is implausible; the margin admits
# cold, clear over-irradiance.
PLAUSIBLE_CAPACITY_FACTOR = 1.25

type ProductionUnit = Literal["MWh", "kWh"]
type ProductionAction = Literal["set", "revert"]
type ProductionOrigin = Literal["editor", "csv_import", "change_request"]
SOURCE_LABELS = ("settlement", "provisional", "manual")


@dataclass(frozen=True)
class ObservationInput:
    starts_at: datetime
    ends_at: datetime
    energy: float
    quality: str = ObservationQuality.VALID
    source: str = "operator"
    quality_label: bool = False
    source_label: str | None = None


class ObservationImportResult(TypedDict):
    created: int
    existing: int
    count: int
    ids: list[int]


@dataclass(frozen=True)
class ProductionChange:
    starts_at: datetime
    ends_at: datetime
    expected_revision_id: int | None
    action: ProductionAction
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
    replayed: bool = False

    @property
    def unchanged(self) -> bool:
        previous = self.previous
        return (
            not self.replayed
            and previous is not None
            and (previous.energy_mwh, previous.quality, previous.source, previous.is_reverted)
            == (self.energy_mwh, self.quality, self.source, self.reverted)
        )

    @property
    def writes(self) -> bool:
        return not self.replayed and not self.unchanged


@dataclass(frozen=True)
class AuditContext:
    origin: ProductionOrigin = "editor"
    file_name: str = ""
    file_sha256: str = ""


def validate_batch(unit: str, count: int) -> None:
    if unit not in {"MWh", "kWh"}:
        raise ValidationError(UNEXPECTED)
    if not 1 <= count <= PRODUCTION_BATCH_LIMIT:
        raise ValidationError(gettext("Save 1 to 2,000 values at a time."))


def normalize_observation(
    observation: ObservationInput, unit: str, zone: ZoneInfo, now: datetime
) -> tuple[datetime, datetime, float, str, str]:
    start, end = observation.starts_at, observation.ends_at
    if start.utcoffset() is None or end.utcoffset() is None:
        raise ValidationError(UNEXPECTED)
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if end - start not in {timedelta(minutes=30), timedelta(minutes=60)} or end > now:
        raise ValidationError(
            gettext("Enter values only for half hours or hours that have already ended.")
        )
    local = start.astimezone(zone)
    for stamp in (local, end.astimezone(zone)):
        if stamp.minute not in (0, 30) or stamp.second or stamp.microsecond:
            raise ValidationError(
                gettext("Start and end each value on the hour or half hour in plant time.")
            )
    midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), zone)
    if end > midnight.astimezone(UTC):
        raise ValidationError(gettext("A value cannot run past midnight in plant time."))
    value = observation.energy
    if isinstance(value, bool) or not isfinite(value) or value < 0:
        raise ValidationError(gettext("Enter zero or a positive number."))
    if observation.quality not in ObservationQuality.values:
        raise ValidationError(UNEXPECTED)
    source = observation.source.strip()
    if not source or len(source) > 200:
        raise ValidationError(UNEXPECTED)
    return start, end, value / 1000 if unit == "kWh" else value, observation.quality, source


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
        raise ValidationError(DETAILS_CHANGED)
    return configuration


def _replays(
    change: ProductionChange,
    head: ProductionObservation | None,
    configuration: PlantConfiguration,
    values: tuple[float, str, str],
) -> bool:
    if (
        head is None
        or head.supersedes_id != change.expected_revision_id
        or head.configuration_id != configuration.pk
    ):
        return False
    if change.action == "revert":
        return head.is_reverted
    return not head.is_reverted and (head.energy_mwh, head.quality, head.source) == values


def prepare_production_changes(
    configuration: PlantConfiguration,
    unit: str,
    changes: list[ProductionChange],
    *,
    capture_revisions: bool = False,
) -> list[PreparedChange]:
    """Validate a batch against the current heads. A change whose token was already superseded
    by the identical value it asks for is a replay of an applied change, not a conflict; a batch
    is replayed whole or not at all."""
    validate_batch(unit, len(changes))
    if any(change.action not in {"set", "revert"} for change in changes):
        raise ValidationError(UNEXPECTED)
    now, zone = timezone.now(), ZoneInfo(configuration.binding.timezone)
    normalized = [
        normalize_observation(
            ObservationInput(
                change.starts_at, change.ends_at, change.energy, change.quality, change.source
            ),
            unit,
            zone,
            now,
        )
        for change in changes
    ]
    spans = sorted((row[0], row[1]) for row in normalized)
    first = spans[0][0].astimezone(zone).date()
    last = (max(end for _, end in spans) - timedelta(microseconds=1)).astimezone(zone).date()
    if (last - first).days >= 31:
        raise ValidationError(gettext("Save values for at most 31 days at a time."))
    if any(following[0] < previous[1] for previous, following in pairwise(spans)):
        raise ValidationError(OVERLAPPING)
    heads = list(
        ProductionObservation.objects.filter(
            plant_id=configuration.plant_id,
            is_current=True,
            starts_at__lt=max(end for _, end in spans),
            ends_at__gt=spans[0][0],
        ).order_by("starts_at")
    )
    indexed = {(row.starts_at, row.ends_at): row for row in heads}
    live = [row for row in heads if not row.is_reverted]
    live_starts, live_ends = [row.starts_at for row in live], [row.ends_at for row in live]
    result: list[PreparedChange] = []
    for change, (start, end, value, quality, source) in zip(changes, normalized, strict=True):
        previous = indexed.get((start, end))
        replayed = False
        if not capture_revisions and (previous.pk if previous else None) != (
            change.expected_revision_id
        ):
            if not _replays(change, previous, configuration, (value, quality, source)):
                raise ConflictError(ErrorCode.PRODUCTION_REVISION_CHANGED)
            replayed = True
        if previous is not None and previous.configuration_id != configuration.pk:
            raise ValidationError(
                gettext(
                    "This value was saved with earlier technical details and cannot be changed."
                )
            )
        if change.action == "revert" and previous is None:
            raise ValidationError(gettext("There is no entered value to remove."))
        overlapping = live[bisect_right(live_ends, start) : bisect_left(live_starts, end)]
        if any(row is not previous for row in overlapping):
            raise ValidationError(OVERLAPPING)
        if change.action == "revert" and previous is not None:
            value, quality, source = previous.energy_mwh, previous.quality, previous.source
        result.append(
            PreparedChange(
                start, end, value, quality, source, change.action == "revert", previous, replayed
            )
        )
    if any(row.replayed for row in result) and any(row.writes for row in result):
        raise ConflictError(ErrorCode.PRODUCTION_REVISION_CHANGED)
    return result


def capacity_warnings(
    configuration: PlantConfiguration, rows: list[PreparedChange]
) -> list[dict[str, Any]]:
    ceiling = min(
        value
        for value in (configuration.dc_capacity_mw, power_limit(configuration))
        if value is not None
    )
    return [
        {"code": "energy_exceeds_capacity", "starts_at": row.starts_at, "ends_at": row.ends_at}
        for row in rows
        if not row.reverted
        and row.energy_mwh / ((row.ends_at - row.starts_at) / timedelta(hours=1))
        > PLAUSIBLE_CAPACITY_FACTOR * ceiling
    ]


def _commit(
    actor: User, configuration: PlantConfiguration, writes: list[PreparedChange]
) -> dict[tuple[datetime, datetime], int]:
    retired = [row.previous.pk for row in writes if row.previous is not None]
    if retired:
        ProductionObservation.objects.filter(pk__in=retired).update(is_current=False)
    saved = ProductionObservation.objects.bulk_create(
        ProductionObservation(
            plant_id=configuration.plant_id,
            configuration=configuration,
            starts_at=row.starts_at,
            ends_at=row.ends_at,
            energy_mwh=row.energy_mwh,
            quality=row.quality,
            source=row.source,
            recorded_by=actor,
            supersedes=row.previous,
            is_reverted=row.reverted,
        )
        for row in writes
    )
    return {(row.starts_at, row.ends_at): row.pk for row in saved}


def _audit(
    audit: AuditContext,
    configuration_id: int,
    created_ids: list[int],
    unchanged: int,
) -> dict[str, object]:
    return {
        "origin": audit.origin,
        "configuration_id": configuration_id,
        "created_ids": created_ids,
        "unchanged": unchanged,
        "file_name": audit.file_name or None,
        "file_sha256": audit.file_sha256 or None,
    }


def change_production(
    actor: User,
    tenant_id: int,
    region_id: int,
    plant_id: int,
    *,
    configuration_id: int,
    unit: str,
    changes: list[ProductionChange],
    audit: AuditContext | None = None,
    confirm_implausible: bool = False,
) -> ObservationImportResult:
    """Apply a batch atomically. A replayed batch answers with the body of its first application
    and changes nothing; values above the plant's capacity need `confirm_implausible`."""
    with transaction.atomic():
        tenant_membership(actor, tenant_id, role=WorkspaceRole.ADMIN, lock=True)
        configuration = lock_production_configuration(
            tenant_id, region_id, plant_id, configuration_id
        )
        prepared = prepare_production_changes(configuration, unit, changes)
        writes = [row for row in prepared if row.writes]
        if not confirm_implausible and capacity_warnings(configuration, writes):
            raise DomainError(ErrorCode.PRODUCTION_IMPLAUSIBLE, field="changes")
        saved = _commit(actor, configuration, writes)
        ids = [
            saved[(row.starts_at, row.ends_at)]
            if row.writes
            else cast(ProductionObservation, row.previous).pk
            for row in prepared
        ]
        unchanged = sum(row.unchanged for row in prepared)
        if writes:
            record_plant_event(
                plant_id=plant_id,
                actor=actor,
                action=PlantEventAction.PRODUCTION_CHANGED,
                details=_audit(
                    audit or AuditContext(), configuration_id, list(saved.values()), unchanged
                ),
            )
            request_preparation([plant_id])
        return {
            "created": len(prepared) - unchanged,
            "existing": unchanged,
            "count": len(ids),
            "ids": ids,
        }


def _hour_span(rows: list[PreparedChange]) -> tuple[datetime, datetime]:
    start = min(row.starts_at for row in rows).replace(minute=0, second=0, microsecond=0)
    last = max(row.ends_at for row in rows)
    end = last.replace(minute=0, second=0, microsecond=0)
    return start, end + timedelta(hours=1) if end < last else end


def production_change_warnings(
    configuration: PlantConfiguration, prepared: list[PreparedChange]
) -> list[dict[str, Any]]:
    start, end = _hour_span(prepared)
    providers = select_provider_measurements(configuration, start, end, timezone.now())[0]
    replaced = {row.previous.pk for row in prepared if row.previous is not None}
    active = observation_heads(cutoff=timezone.now()).filter(
        configuration=configuration,
        is_reverted=False,
        starts_at__lt=end,
        ends_at__gt=start,
    )
    manual_spans = sorted(
        [(row.starts_at, row.ends_at) for row in active if row.pk not in replaced]
        + [(row.starts_at, row.ends_at) for row in prepared if not row.reverted]
    )
    changed = sorted((row.starts_at, row.ends_at) for row in prepared)
    return [
        {"code": "provider_hour_incomplete", "starts_at": row.starts_at, "ends_at": row.ends_at}
        for row in providers
        if _overlaps(changed, row.starts_at, row.ends_at)
        and _overlaps(manual_spans, row.starts_at, row.ends_at)
        and not _covers(manual_spans, row.starts_at, row.ends_at)
    ]


def _overlaps(spans: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> bool:
    # `spans` are sorted and disjoint, so only the last one starting before `end` can overlap.
    index = bisect_left(spans, (end,))
    return index > 0 and spans[index - 1][1] > start


def _covers(spans: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> bool:
    cursor = start
    for begin, finish in spans:
        if finish <= cursor:
            continue
        if begin > cursor:
            return False
        cursor = finish
        if cursor >= end:
            return True
    return False


def _as_shown(
    row: PreparedChange, item: ObservationInput, shown: Measurement | None
) -> PreparedChange:
    # A file shows a derived status and only the kind of source, never the saved text.
    saved = row.previous
    if (
        saved is None
        or saved.is_reverted
        or shown is None
        or shown.snapshot is not None
        or shown.pk != saved.pk
    ):
        return row
    return replace(
        row,
        quality=saved.quality
        if item.quality_label and row.quality == shown.quality
        else row.quality,
        source=saved.source if item.source_label == "manual" else row.source,
    )


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
    """Read-only: takes no row lock; the commit revalidates every token it returns."""
    with read_snapshot():
        tenant_membership(actor, tenant_id, role=WorkspaceRole.ADMIN)
        configuration = lock_production_configuration(
            tenant_id, region_id, plant_id, configuration_id, lock=False
        )
        changes = [
            ProductionChange(
                item.starts_at, item.ends_at, None, "set", item.energy, item.quality, item.source
            )
            for item in observations
        ]
        prepared = prepare_production_changes(configuration, unit, changes, capture_revisions=True)
        start, end = _hour_span(prepared)
        current = select_measurements(configuration, start, end, timezone.now())[0]
        shown = {(row.starts_at, row.ends_at): row for row in current}
        current_values = {key: row.energy_mwh for key, row in shown.items()}
        prepared = [
            _as_shown(row, item, shown.get((row.starts_at, row.ends_at)))
            for row, item in zip(prepared, observations, strict=True)
        ]
        return {
            "changes": [
                {
                    "starts_at": row.starts_at,
                    "ends_at": row.ends_at,
                    "expected_revision_id": ready.previous.pk if ready.previous else None,
                    "action": row.action,
                    "energy": row.energy,
                    "quality": ready.quality,
                    "source": ready.source,
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
            "warnings": production_change_warnings(configuration, prepared)
            + capacity_warnings(configuration, prepared),
        }


def import_observations(
    actor: User,
    tenant_id: int,
    region_id: int,
    plant_id: int,
    *,
    configuration_id: int,
    unit: str,
    observations: list[ObservationInput],
) -> ObservationImportResult:
    validate_batch(unit, len(observations))
    with transaction.atomic():
        tenant_membership(actor, tenant_id, role=WorkspaceRole.ADMIN, lock=True)
        region = Region.objects.active().select_for_update().get(pk=region_id, tenant_id=tenant_id)
        plant = Plant.objects.select_for_update().get(pk=plant_id, region=region)
        configuration = PlantConfiguration.objects.select_related("binding").get(
            pk=configuration_id, plant=plant
        )
        now, zone = timezone.now(), ZoneInfo(configuration.binding.timezone)
        created = 0
        ids: list[int] = []
        references: dict[date, tuple[PowerInterval, ...]] = {}
        for item in observations:
            start, end, value, quality, source = normalize_observation(item, unit, zone, now)
            overlap = ProductionObservation.objects.filter(
                plant=plant, starts_at__lt=end, ends_at__gt=start, is_current=True
            ).first()
            if overlap is not None:
                if (
                    overlap.starts_at != start
                    or overlap.ends_at != end
                    or overlap.configuration_id != configuration.pk
                    or overlap.energy_mwh != value
                    or overlap.quality != quality
                    or overlap.source != source
                    or overlap.is_reverted
                ):
                    raise ValidationError(OVERLAPPING)
                ids.append(overlap.pk)
                continue
            if not configuration_is_current(configuration):
                raise ValidationError(DETAILS_CHANGED)
            day = start.astimezone(zone).date()
            if day not in references:
                references[day] = reference_intervals(configuration, day, now)
            if not interval_is_covered(references[day], start, end):
                raise DomainError(ErrorCode.ESTIMATED_ACTUALS_UNAVAILABLE)
            saved = ProductionObservation.objects.create(
                plant=plant,
                configuration=configuration,
                starts_at=start,
                ends_at=end,
                energy_mwh=value,
                quality=quality,
                source=source,
                recorded_by=actor,
            )
            ids.append(saved.pk)
            created += 1
        if created:
            record_plant_event(
                plant_id=plant.pk,
                actor=actor,
                action=PlantEventAction.OBSERVATIONS_IMPORTED,
                details={"configuration_id": configuration.pk, "created": created, "ids": ids},
            )
            request_preparation([plant.pk])
        return {"created": created, "existing": len(ids) - created, "count": len(ids), "ids": ids}
