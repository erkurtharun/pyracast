from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import fsum
from typing import Any, cast
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext

from pyracast.accounts.models import Membership, WorkspaceRole
from pyracast.errors import UNEXPECTED
from pyracast.irradiance import local_day_bounds
from pyracast.metering.models import EpiasSnapshot
from pyracast.plants.availability import (
    available_date_bounds,
    interval_is_covered,
    reference_intervals,
)
from pyracast.plants.measurements import (
    Measurement,
    select_production_measurements,
)
from pyracast.plants.models import Plant, PlantConfiguration, ProductionObservation
from pyracast.plants.operations import configuration_is_current
from pyracast.plants.serializers import ObservationOutput
from pyracast.solar import DAYLIGHT_POLICY, daylight_window


def _coverage(rows: list[Measurement], start: datetime, end: datetime) -> bool:
    cursor = start
    for row in rows:
        if row.ends_at <= cursor:
            continue
        if row.starts_at > cursor or row.quality == "invalid":
            return False
        cursor = row.ends_at
        if cursor >= end:
            return True
    return False


def _value(row: Measurement) -> dict[str, Any]:
    return {
        "energy_mwh": row.energy_mwh,
        "quality": row.quality,
        "source": row.source,
        "source_kind": row.snapshot.source_kind if row.snapshot else "manual",
        "provisional": row.snapshot is not None
        and row.snapshot.source_kind == "realtime_generation",
        "aggregated": False,
    }


def slot_value(rows: list[Measurement], start: datetime, end: datetime) -> dict[str, Any] | None:
    contained = [row for row in rows if row.starts_at >= start and row.ends_at <= end]
    if len(contained) == 1 and contained[0].starts_at == start and contained[0].ends_at == end:
        return _value(contained[0])
    if len(contained) != 2:
        return None
    try:
        energy = fsum(row.energy_mwh for row in contained)
    except OverflowError:
        return None
    qualities = {row.quality for row in contained}
    return {
        "energy_mwh": energy,
        "quality": next(iter(qualities)) if len(qualities) == 1 else "mixed",
        "source": "multiple_manual",
        "source_kind": "manual",
        "provisional": False,
        "aggregated": True,
    }


def _total(
    rows: list[Measurement], required: tuple[tuple[datetime, datetime], ...]
) -> float | None:
    if not all(_coverage(rows, begin, finish) for begin, finish in required):
        return None
    try:
        return fsum(row.energy_mwh for row in rows if row.quality != "invalid")
    except OverflowError:
        return None


def _provider(row: Measurement) -> dict[str, Any]:
    snapshot = cast(EpiasSnapshot, row.snapshot)
    return {
        "starts_at": row.starts_at,
        "ends_at": row.ends_at,
        "energy_mwh": row.energy_mwh,
        "source_kind": snapshot.source_kind,
        "snapshot_id": snapshot.pk,
        "collected_at": snapshot.collected_at,
        "available_at": snapshot.available_at,
        "content_sha256": snapshot.content_sha256,
        "provisional": snapshot.source_kind == "realtime_generation",
    }


def _slots(
    rows: list[Measurement],
    start: datetime,
    end: datetime,
    resolution_minutes: int,
    native_intervals: bool,
) -> list[tuple[datetime, datetime]]:
    result = []
    cursor = start
    while cursor < end:
        if native_intervals:
            following = next((row for row in rows if row.starts_at >= cursor), None)
            if following is not None and following.starts_at == cursor:
                finish = following.ends_at
            else:
                next_hour = start + timedelta(hours=(cursor - start) // timedelta(hours=1) + 1)
                finish = min(end, next_hour, following.starts_at if following else end)
        else:
            finish = min(cursor + timedelta(minutes=resolution_minutes), end)
        result.append((cursor, finish))
        cursor = finish
    return result


@dataclass(frozen=True)
class NativeSlot:
    starts_at: datetime
    ends_at: datetime
    effective: dict[str, Any] | None


@dataclass(frozen=True)
class NativeProductionDay:
    date: date
    total_mwh: float | None
    complete: bool
    slots: tuple[NativeSlot, ...]


@dataclass(frozen=True)
class NativeProduction:
    timezone: str
    days: tuple[NativeProductionDay, ...]


def native_production_days(
    plant: Plant, start_date: date, end_date: date, now: datetime
) -> NativeProduction:
    configuration = (
        PlantConfiguration.objects.select_related("binding__provider_account").get(
            pk=plant.current_configuration_id
        )
        if plant.current_configuration_id is not None
        else None
    )
    location = configuration.binding if configuration else plant.region
    zone = location.timezone
    range_start, _ = local_day_bounds(start_date, zone)
    _, range_end = local_day_bounds(end_date, zone)
    effective = select_production_measurements(
        plant, configuration, range_start, range_end, now
    ).selection.effective
    days = []
    day = start_date
    while day <= end_date:
        start, end = local_day_bounds(day, zone)
        rows = [row for row in effective if row.starts_at >= start and row.ends_at <= end]
        daylight = daylight_window(day, zone, location.latitude, location.longitude)
        days.append(
            NativeProductionDay(
                date=day,
                total_mwh=_total(rows, daylight.windows or ((start, end),)),
                complete=_coverage(rows, start, end),
                slots=tuple(
                    NativeSlot(begin, finish, slot_value(rows, begin, finish))
                    for begin, finish in _slots(rows, start, end, 60, native_intervals=True)
                ),
            )
        )
        day += timedelta(days=1)
    return NativeProduction(zone, tuple(days))


def _role(actor: User | None, plant: Plant) -> str | None:
    if actor is None:
        return None
    return (
        Membership.objects.filter(
            user=actor, user__is_active=True, tenant_id=plant.region.tenant_id
        )
        .values_list("role", flat=True)
        .first()
    )


def _earlier_configuration_heads(
    heads: list[ProductionObservation], configuration: PlantConfiguration | None
) -> list[tuple[ProductionObservation, dict[str, Any]]]:
    """Live corrections recorded under an earlier configuration; they are shown beside every
    interval they overlap but never enter its effective value or the day total."""
    earlier = [
        row
        for row in heads
        if not row.is_reverted
        and (configuration is None or row.configuration_id != configuration.pk)
    ]
    revisions = dict(
        PlantConfiguration.objects.filter(
            pk__in={row.configuration_id for row in earlier}
        ).values_list("pk", "revision")
    )
    return [
        (
            row,
            ObservationOutput(row).data
            | {"configuration_revision": revisions[row.configuration_id]},
        )
        for row in earlier
    ]


def production_history(
    plant: Plant,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    resolution_minutes: int = 60,
    daylight_only: bool = False,
    native_intervals: bool = False,
    actor: User | None = None,
) -> dict[str, Any]:
    configuration = (
        PlantConfiguration.objects.select_related("binding__provider_account").get(
            pk=plant.current_configuration_id
        )
        if plant.current_configuration_id is not None
        else None
    )
    zone = configuration.binding.timezone if configuration else plant.region.timezone
    now = timezone.now()
    first, last = available_date_bounds(configuration, now) if configuration else (None, None)
    if (start_date is None) != (end_date is None):
        raise ValidationError(gettext("Enter both the start and the end date."))
    if start_date is None or end_date is None:
        end_date = last or now.astimezone(ZoneInfo(zone)).date() - timedelta(days=1)
        start_date = (
            end_date
            if daylight_only
            else max(first or end_date - timedelta(days=6), end_date - timedelta(days=6))
        )
    if resolution_minutes not in {30, 60}:
        raise ValidationError(UNEXPECTED)
    if not 1 <= (end_date - start_date).days + 1 <= 31:
        raise ValidationError(gettext("Choose a date range of 1 to 31 days."))
    range_start, _ = local_day_bounds(start_date, zone)
    _, range_end = local_day_bounds(end_date, zone)
    selected = select_production_measurements(plant, configuration, range_start, range_end, now)
    heads = selected.heads
    observations = [row for row in heads if not row.is_reverted]
    encoded = {row["id"]: row for row in ObservationOutput(observations, many=True).data}
    earlier = _earlier_configuration_heads(heads, configuration)
    providers, effective = selected.selection.providers, selected.selection.effective
    current = configuration is not None and configuration_is_current(configuration)
    role = _role(actor, plant)
    may_edit = actor is None or role == WorkspaceRole.ADMIN
    may_propose = role == WorkspaceRole.VIEWER
    days, intervals = [], []
    day = start_date
    while day <= end_date:
        location = configuration.binding if configuration else plant.region
        daylight = daylight_window(day, zone, location.latitude, location.longitude)
        native = reference_intervals(configuration, day, now) if configuration else ()
        cursor, day_end = local_day_bounds(day, zone)
        day_rows = [row for row in effective if row.starts_at >= cursor and row.ends_at <= day_end]
        complete = _coverage(day_rows, cursor, day_end)
        required = daylight.windows or ((cursor, day_end),)
        total = _total(day_rows, required)
        available_count = recorded_count = missing_count = 0
        slots = _slots(day_rows, cursor, day_end, resolution_minutes, native_intervals)
        for cursor, end in slots:
            if daylight_only and not any(
                cursor < finish and end > begin for begin, finish in daylight.windows
            ):
                continue
            overlaps = [
                row
                for row in observations
                if row.starts_at < end
                and row.ends_at > cursor
                and (not native_intervals or row.configuration_id == plant.current_configuration_id)
            ]
            exact = next(
                (row for row in overlaps if row.starts_at == cursor and row.ends_at == end), None
            )
            head = next(
                (row for row in heads if row.starts_at == cursor and row.ends_at == end), None
            )
            originals = [row for row in providers if row.starts_at < end and row.ends_at > cursor]
            previous_configuration = native_intervals and any(
                row.configuration_id != plant.current_configuration_id
                and row.starts_at < end
                and row.ends_at > cursor
                and (not row.is_reverted or (row.starts_at == cursor and row.ends_at == end))
                for row in heads
            )
            aligned = end - cursor in {timedelta(minutes=30), timedelta(minutes=60)} and all(
                stamp.astimezone(ZoneInfo(zone)).minute in {0, 30}
                and not (stamp.second or stamp.microsecond)
                for stamp in (cursor, end)
            )
            technical_reason = (
                "configuration_unavailable"
                if configuration is None
                else "configuration_not_current"
                if not current
                else "configuration_mismatch"
                if previous_configuration
                or (head is not None and head.configuration_id != configuration.pk)
                else "observation_overlap"
                if overlaps and exact is None
                else "interval_not_completed"
                if end > now
                else "unsupported_interval"
                if not aligned
                else None
            )
            edit_reason = "workspace_readonly" if not may_edit else technical_reason
            available = interval_is_covered(native, cursor, end) and end <= now
            intervals.append(
                {
                    "starts_at": cursor,
                    "ends_at": end,
                    "observation": encoded[exact.pk] if exact else None,
                    "previous_configuration_observations": [
                        data
                        for row, data in earlier
                        if row.starts_at < end and row.ends_at > cursor
                    ],
                    "revision_id": head.pk if head else None,
                    "effective": slot_value(effective, cursor, end),
                    "provider_originals": [_provider(row) for row in originals],
                    "can_edit": edit_reason is None,
                    "can_propose": may_propose and technical_reason is None,
                    "can_propose_revert": may_propose
                    and technical_reason is None
                    and exact is not None,
                    "proposal_unavailable_reason": technical_reason,
                    "can_revert": edit_reason is None and exact is not None,
                    "edit_unavailable_reason": edit_reason,
                }
            )
            available_count += available
            recorded_count += bool(overlaps)
            missing_count += available and not overlaps
        days.append(
            {
                "date": day,
                "available_count": available_count,
                "recorded_count": recorded_count,
                "missing_count": missing_count,
                "sunrise": daylight.sunrise,
                "sunset": daylight.sunset,
                "daylight_status": daylight.status,
                "daylight_windows": [
                    {"starts_at": begin, "ends_at": finish} for begin, finish in daylight.windows
                ],
                "total_mwh": total,
                "complete": complete,
            }
        )
        day += timedelta(days=1)
    return {
        "plant_id": plant.pk,
        "configuration_id": configuration.pk if configuration else None,
        "timezone": zone,
        "available_start_date": first,
        "available_end_date": last,
        "start_date": start_date,
        "end_date": end_date,
        "resolution_minutes": resolution_minutes,
        "native_intervals": native_intervals,
        "slot_scope": "daylight" if daylight_only else "full_day",
        "daylight_policy": DAYLIGHT_POLICY,
        "days": days,
        "intervals": intervals,
    }
