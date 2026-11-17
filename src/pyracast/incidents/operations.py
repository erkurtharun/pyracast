from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import OuterRef, Subquery
from django.utils import timezone
from django.utils.translation import gettext, gettext_lazy
from rest_framework.exceptions import APIException

from pyracast.accounts.authorization import tenant_membership
from pyracast.accounts.models import Membership
from pyracast.incidents.models import IncidentRevision, PlantIncident
from pyracast.plants.models import Plant, PlantConfiguration
from pyracast.regions.models import Region


class RevisionConflict(APIException):
    status_code = 409
    default_detail = gettext_lazy("Incident revision changed.")


@dataclass(frozen=True)
class IncidentEvidence:
    incident_id: int
    revision_id: int
    revision: int
    recorded_at: datetime
    kind: str
    title: str
    notes: str
    starts_at: datetime
    ends_at: datetime | None
    available_dc_mw: float | None
    available_inverter_mw: float | None
    available_export_mw: float | None
    declared_configuration_id: int
    cancelled_at: datetime | None


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


def _evidence(revision: IncidentRevision) -> IncidentEvidence:
    data = revision.snapshot
    return IncidentEvidence(
        incident_id=revision.incident_id,
        revision_id=revision.pk,
        revision=revision.revision,
        recorded_at=revision.recorded_at,
        kind=data["kind"],
        title=data["title"],
        notes=data["notes"],
        starts_at=datetime.fromisoformat(data["starts_at"]),
        ends_at=datetime.fromisoformat(data["ends_at"]) if data["ends_at"] else None,
        available_dc_mw=data["available_dc_mw"],
        available_inverter_mw=data["available_inverter_mw"],
        available_export_mw=data["available_export_mw"],
        declared_configuration_id=data["declared_configuration_id"],
        cancelled_at=datetime.fromisoformat(data["cancelled_at"]) if data["cancelled_at"] else None,
    )


def pinned_incidents(plant_id: int, revision_ids: list[int]) -> tuple[IncidentEvidence, ...]:
    revisions = list(
        IncidentRevision.objects.filter(incident__plant_id=plant_id, pk__in=revision_ids).order_by(
            "incident_id"
        )
    )
    if len(revisions) != len(revision_ids):
        raise ValueError("Incident revision evidence is missing or belongs to another plant.")
    return tuple(_evidence(revision) for revision in revisions)


def select_incidents(
    plant_id: int, start: datetime, end: datetime, known_at: datetime
) -> tuple[IncidentEvidence, ...]:
    if any(x.utcoffset() is None for x in (start, end, known_at)) or end <= start:
        raise ValueError("An aware, nonempty interval and aware knowledge time are required.")
    latest = (
        IncidentRevision.objects.filter(
            incident_id=OuterRef("incident_id"), recorded_at__lte=known_at
        )
        .order_by("-revision")
        .values("pk")[:1]
    )
    revisions = IncidentRevision.objects.filter(
        incident__plant_id=plant_id, recorded_at__lte=known_at, pk=Subquery(latest)
    ).order_by("incident_id")
    result = []
    for revision in revisions:
        evidence = _evidence(revision)
        if (
            evidence.cancelled_at
            or evidence.starts_at >= end
            or (evidence.ends_at is not None and evidence.ends_at <= start)
        ):
            continue
        result.append(evidence)
    return tuple(result)


def lock_incident_plant(
    tenant_id: int, region_id: int, plant_id: int, *, lock: bool = True
) -> Plant:
    regions = Region.objects.all()
    plants = Plant.objects.select_related("current_configuration")
    if lock:
        regions = regions.select_for_update()
        plants = plants.select_for_update(of=("self",))
    regions.get(pk=region_id, tenant_id=tenant_id, deleted_at__isnull=True)
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
        raise ValidationError(gettext("Incident fields are incomplete or unsupported."))
    start, end = values["starts_at"], values["ends_at"]
    if (
        start.utcoffset() is None
        or start > now
        or (end is not None and (end.utcoffset() is None or end <= start or end > now))
    ):
        raise ValidationError(
            gettext(
                "Use aware timestamps: start must not be in the future; "
                "end must be after start and no later than now."
            )
        )
    if values["kind"] not in {"inverter", "panels", "grid", "other", "multiple"}:
        raise ValidationError(gettext("Unsupported incident kind."))
    if (
        not isinstance(values["title"], str)
        or not values["title"].strip()
        or len(values["title"]) > 200
        or not isinstance(values["notes"], str)
        or len(values["notes"]) > 2000
    ):
        raise ValidationError(
            gettext(
                "A title of at most 200 characters and notes of at most "
                "2000 characters are required."
            )
        )
    for field, baseline in (
        ("available_dc_mw", configuration.dc_capacity_mw),
        ("available_inverter_mw", configuration.inverter_capacity_mw),
        ("available_export_mw", configuration.export_limit_mw),
    ):
        value = values[field]
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or value < 0
            or (baseline is not None and value > baseline)
        ):
            raise ValidationError(
                gettext(
                    "%(field)s must be finite, nonnegative, "
                    "and no greater than the known configured capacity."
                ),
                params={"field": field},
            )


def _record(row: PlantIncident, actor: User) -> None:
    IncidentRevision.objects.create(
        incident=row, revision=row.revision, snapshot=_snapshot(row), actor=actor
    )
    from pyracast.plants.incident_policy import incidents_changed

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
            raise ValidationError(gettext("A plant configuration is required."))
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
            raise RevisionConflict
        if row.cancelled_at is not None:
            raise ValidationError(gettext("A cancelled incident cannot be changed."))
        configuration = (
            plant.current_configuration if row.ends_at is None else row.declared_configuration
        )
        if configuration is None:
            raise ValidationError(gettext("A plant configuration is required."))
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
    from django.db.models import Q

    if overlap.filter(Q(ends_at__isnull=True) | Q(ends_at__gt=values["starts_at"])).exists():
        raise ValidationError(gettext("Incident periods must not overlap."))
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
        tenant_membership(actor, tenant_id, role=Membership.Role.ADMIN, lock=True)
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
        raise RevisionConflict
    if row.cancelled_at is not None or (not cancel and row.ends_at is not None):
        raise ValidationError(gettext("The incident is already closed."))
    now = timezone.now()
    if cancel:
        row.cancelled_at = now
    elif (
        ends_at is None or ends_at.utcoffset() is None or ends_at <= row.starts_at or ends_at > now
    ):
        raise ValidationError(gettext("End must be aware, after start and no later than now."))
    else:
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
        tenant_membership(actor, tenant_id, role=Membership.Role.ADMIN, lock=True)
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
