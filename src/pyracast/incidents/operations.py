from datetime import datetime
from math import isfinite
from typing import Any

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from pyracast.accounts.authorization import tenant_membership
from pyracast.accounts.models import WorkspaceRole
from pyracast.errors import ConflictError, DomainError, ErrorCode
from pyracast.incidents.models import IncidentKind, IncidentRevision, PlantIncident
from pyracast.plants.incident_policy import incidents_changed
from pyracast.plants.models import Plant, PlantConfiguration
from pyracast.regions.models import Region

UNZONED = _("Outage times must include a time zone.")


def _snapshot(row: PlantIncident) -> dict[str, object]:
    return {
        "kind": row.kind,
        "title": row.title,
        "notes": row.notes,
        "starts_at": row.starts_at.isoformat(),
        "ends_at": row.ends_at.isoformat() if row.ends_at else None,
        "available_dc_mw": row.available_dc_mw,
        "available_inverter_mw": row.available_inverter_mw,
        "available_export_mw": row.available_export_mw,
        "declared_configuration_id": row.declared_configuration_id,
        "cancelled_at": row.cancelled_at.isoformat() if row.cancelled_at else None,
    }


def lock_incident_plant(
    tenant_id: int, region_id: int, plant_id: int, *, lock: bool = True
) -> Plant:
    regions = Region.objects.active()
    plants = Plant.objects.select_related("current_configuration")
    if lock:
        regions = regions.select_for_update()
        plants = plants.select_for_update(of=("self",))
    regions.get(pk=region_id, tenant_id=tenant_id)
    return plants.get(pk=plant_id, region_id=region_id)


def _validate(values: dict[str, Any], configuration: PlantConfiguration, now: datetime) -> None:
    if values.keys() != {
        "kind",
        "title",
        "notes",
        "starts_at",
        "ends_at",
        "available_dc_mw",
        "available_inverter_mw",
        "available_export_mw",
    }:
        raise DomainError(
            ErrorCode.INVALID_REQUEST, _("Outage fields are incomplete or unsupported.")
        )
    start, end = values["starts_at"], values["ends_at"]
    if start.utcoffset() is None:
        raise DomainError(ErrorCode.INVALID_REQUEST, UNZONED, field="starts_at")
    if start > now:
        raise DomainError(ErrorCode.INCIDENT_STARTS_IN_FUTURE, field="starts_at")
    if end is not None:
        _validate_end(start, end, now)
    if values["kind"] not in IncidentKind.values:
        raise DomainError(ErrorCode.INVALID_REQUEST, _("Unsupported outage kind."), field="kind")
    title, notes = values["title"], values["notes"]
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise DomainError(
            ErrorCode.INVALID_REQUEST,
            _("A title of at most 200 characters is required."),
            field="title",
        )
    if not isinstance(notes, str) or len(notes) > 2000:
        raise DomainError(
            ErrorCode.INVALID_REQUEST, _("Notes must stay within 2000 characters."), field="notes"
        )
    for field, baseline in (
        ("available_dc_mw", configuration.dc_capacity_mw),
        ("available_inverter_mw", configuration.inverter_capacity_mw),
        ("available_export_mw", configuration.export_limit_mw),
    ):
        value = values[field]
        if value is None:
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or value < 0
        ):
            raise DomainError(
                ErrorCode.INVALID_REQUEST,
                _("Available capacity must be a finite, nonnegative number."),
                field=field,
            )
        if baseline is not None and value > baseline:
            raise DomainError(ErrorCode.INCIDENT_CAPACITY_EXCEEDED, field=field)


def _validate_end(start: datetime, end: datetime, now: datetime) -> None:
    if end.utcoffset() is None:
        raise DomainError(ErrorCode.INVALID_REQUEST, UNZONED, field="ends_at")
    if end <= start:
        raise DomainError(ErrorCode.INCIDENT_ENDS_BEFORE_START, field="ends_at")
    if end > now:
        raise DomainError(ErrorCode.INCIDENT_ENDS_IN_FUTURE, field="ends_at")


def _record(row: PlantIncident, actor: User) -> None:
    IncidentRevision.objects.create(
        incident=row, revision=row.revision, snapshot=_snapshot(row), actor=actor
    )
    incidents_changed(row.plant_id)


def prepare_incident(
    actor: User,
    plant: Plant,
    *,
    incident_id: int | None = None,
    expected_revision: int | None = None,
    values: dict[str, Any],
    lock: bool = True,
) -> PlantIncident:
    if incident_id is None:
        configuration = plant.current_configuration
        if configuration is None:
            raise DomainError(ErrorCode.CONFIGURATION_UNAVAILABLE)
        row = PlantIncident(
            plant=plant,
            declared_configuration=configuration,
            created_by=actor,
            updated_by=actor,
        )
    else:
        rows = PlantIncident.objects.select_related("declared_configuration")
        if lock:
            rows = rows.select_for_update()
        row = rows.get(pk=incident_id, plant=plant)
        if expected_revision != row.revision:
            raise ConflictError(ErrorCode.INCIDENT_REVISION_CHANGED)
        if row.cancelled_at is not None:
            raise DomainError(ErrorCode.INCIDENT_CLOSED)
        configuration = (
            plant.current_configuration
            if values.get("ends_at") is None
            else row.declared_configuration
        )
        if configuration is None:
            raise DomainError(ErrorCode.CONFIGURATION_UNAVAILABLE)
        row.declared_configuration = configuration
        row.revision += 1
        row.updated_by = actor
    _validate(values, configuration, timezone.now())
    overlap = (
        PlantIncident.objects.filter(plant=plant, cancelled_at__isnull=True)
        .exclude(pk=row.pk)
        .filter(starts_at__lt=values["ends_at"])
        if values["ends_at"] is not None
        else PlantIncident.objects.filter(plant=plant, cancelled_at__isnull=True).exclude(pk=row.pk)
    )
    if overlap.filter(Q(ends_at__isnull=True) | Q(ends_at__gt=values["starts_at"])).exists():
        raise DomainError(ErrorCode.INCIDENT_OVERLAP, field="starts_at")
    for name, value in values.items():
        setattr(row, name, value)
    return row


def save_incident(
    actor: User,
    tenant_id: int,
    region_id: int,
    plant_id: int,
    *,
    incident_id: int | None = None,
    expected_revision: int | None = None,
    **values: Any,
) -> PlantIncident:
    with transaction.atomic():
        tenant_membership(actor, tenant_id, role=WorkspaceRole.ADMIN, lock=True)
        plant = lock_incident_plant(tenant_id, region_id, plant_id)
        row = prepare_incident(
            actor,
            plant,
            incident_id=incident_id,
            expected_revision=expected_revision,
            values=values,
        )
        row.save()
        _record(row, actor)
        return row


def prepare_incident_finish(
    actor: User,
    plant: Plant,
    *,
    incident_id: int,
    expected_revision: int,
    ends_at: datetime | None = None,
    cancel: bool = False,
    lock: bool = True,
) -> PlantIncident:
    rows = PlantIncident.objects.all()
    if lock:
        rows = rows.select_for_update()
    row = rows.get(pk=incident_id, plant=plant)
    if row.revision != expected_revision:
        raise ConflictError(ErrorCode.INCIDENT_REVISION_CHANGED)
    if row.cancelled_at is not None or (not cancel and row.ends_at is not None):
        raise DomainError(ErrorCode.INCIDENT_CLOSED)
    now = timezone.now()
    if cancel:
        row.cancelled_at = now
    elif ends_at is None:
        raise DomainError(
            ErrorCode.INVALID_REQUEST, _("An outage end time is required."), field="ends_at"
        )
    else:
        _validate_end(row.starts_at, ends_at, now)
        row.ends_at = ends_at
    row.revision += 1
    row.updated_by = actor
    return row


def finish_incident(
    actor: User,
    tenant_id: int,
    region_id: int,
    plant_id: int,
    *,
    incident_id: int,
    expected_revision: int,
    ends_at: datetime | None = None,
    cancel: bool = False,
) -> PlantIncident:
    with transaction.atomic():
        tenant_membership(actor, tenant_id, role=WorkspaceRole.ADMIN, lock=True)
        plant = lock_incident_plant(tenant_id, region_id, plant_id)
        row = prepare_incident_finish(
            actor,
            plant,
            incident_id=incident_id,
            expected_revision=expected_revision,
            ends_at=ends_at,
            cancel=cancel,
        )
        row.save()
        _record(row, actor)
        return row
