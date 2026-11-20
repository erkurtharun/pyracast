from dataclasses import dataclass
from datetime import datetime

from django.db.models import OuterRef, Subquery

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


def _evidence(revision: IncidentRevision) -> IncidentEvidence:
    data = revision.snapshot
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
        available_dc_mw=data["available_dc_mw"],
        available_inverter_mw=data["available_inverter_mw"],
        available_export_mw=data["available_export_mw"],
        declared_configuration_id=data["declared_configuration_id"],
        cancelled_at=_moment(data["cancelled_at"]),
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
    evidence = (_evidence(revision) for revision in revisions)
    return tuple(
        row
        for row in evidence
        if not row.cancelled_at
        and row.starts_at < end
        and (row.ends_at is None or row.ends_at > start)
    )
