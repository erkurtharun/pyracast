from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db.models import OuterRef, Subquery

from pyracast.forecasts.faults import decoding
from pyracast.incidents.models import IncidentRevision


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


def _moment(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _capacity(value: object) -> float | None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int | float)):
        raise TypeError("A stored available capacity is not a number.")
    return value


def _recorded(revision: IncidentRevision) -> dict[str, Any]:
    if not isinstance(revision.snapshot, dict):
        raise TypeError("A stored incident revision is not an object.")
    return revision.snapshot


def _evidence(revision: IncidentRevision) -> IncidentEvidence:
    with decoding("incident_revision", revision.pk):
        data = _recorded(revision)
        return IncidentEvidence(
            incident_id=revision.incident_id,
            revision_id=revision.pk,
            revision=revision.revision,
            recorded_at=revision.created_at,
            kind=data["kind"],
            title=data["title"],
            notes=data["notes"],
            starts_at=datetime.fromisoformat(data["starts_at"]),
            ends_at=_moment(data["ends_at"]),
            available_dc_mw=_capacity(data["available_dc_mw"]),
            available_inverter_mw=_capacity(data["available_inverter_mw"]),
            available_export_mw=_capacity(data["available_export_mw"]),
            declared_configuration_id=data["declared_configuration_id"],
            cancelled_at=_moment(data["cancelled_at"]),
        )


def _active_within(revision: IncidentRevision, start: datetime, end: datetime) -> bool:
    """Decodes only what decides whether the revision is a live outage overlapping the range."""
    with decoding("incident_revision", revision.pk):
        data = _recorded(revision)
        if _moment(data["cancelled_at"]) is not None:
            return False
        ends_at = _moment(data["ends_at"])
        if ends_at is not None and ends_at <= start:
            return False
        return datetime.fromisoformat(data["starts_at"]) < end


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
    """The newest revision of each outage recorded by `known_at` that overlaps [start, end);
    cancelled outages are dropped."""
    if any(x.utcoffset() is None for x in (start, end, known_at)) or end <= start:
        raise ValueError("An aware, nonempty interval and aware knowledge time are required.")
    latest = (
        IncidentRevision.objects.filter(
            incident_id=OuterRef("incident_id"), created_at__lte=known_at
        )
        .order_by("-revision")
        .values("pk")[:1]
    )
    revisions = IncidentRevision.objects.filter(
        incident__plant_id=plant_id, created_at__lte=known_at, pk=Subquery(latest)
    ).order_by("incident_id")
    return tuple(
        _evidence(revision) for revision in revisions if _active_within(revision, start, end)
    )
