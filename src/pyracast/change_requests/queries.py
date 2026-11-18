from datetime import datetime
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from django.db.models import Count, Max, Min, OuterRef, QuerySet, Subquery

from pyracast.accounts.authorization import tenant_membership
from pyracast.accounts.models import Membership, WorkspaceRole
from pyracast.change_requests.models import (
    DECIDED,
    INCIDENT_EDITS,
    ChangeRequest,
    ChangeRequestKind,
    ChangeRequestStatus,
    IncidentChangeAction,
)
from pyracast.change_requests.operations import (
    _request,
    _validate,
    visible_requests,
)
from pyracast.change_requests.preparation import (
    digest,
    incident_values,
    production_context,
    review_context,
)
from pyracast.errors import MESSAGES, ErrorCode
from pyracast.incidents.models import IncidentRevision
from pyracast.incidents.operations import lock_incident_plant


def inbox(
    actor: User, tenant_id: int, *, scope: str = "pending", plant_id: int | None = None
) -> QuerySet[ChangeRequest]:
    membership = tenant_membership(actor, tenant_id)
    rows = visible_requests(actor, membership)
    if membership.role != WorkspaceRole.ADMIN or scope == "mine":
        rows = rows.filter(proposed_by=actor, proposer_membership_id=membership.pk)
    if scope == "pending":
        rows = rows.filter(status=ChangeRequestStatus.PENDING)
    elif scope == "history":
        rows = rows.exclude(status=ChangeRequestStatus.PENDING)
    if plant_id is not None:
        rows = rows.filter(plant_id=plant_id)
    return _with_triage(rows).order_by("-submitted_at", "-pk")


def _with_triage(rows: QuerySet[ChangeRequest]) -> QuerySet[ChangeRequest]:
    baseline = IncidentRevision.objects.filter(
        incident_id=OuterRef("incident_change__incident_id"),
        revision=OuterRef("incident_change__expected_revision"),
    ).values("snapshot")
    return rows.select_related(
        "plant__region",
        "configuration__binding",
        "proposed_by",
        "reviewed_by",
        "incident_change",
    ).annotate(
        item_count=Count("production_items"),
        period_start=Min("production_items__starts_at"),
        period_end=Max("production_items__ends_at"),
        baseline=Subquery(baseline),
    )


def _moment(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _triage(row: Any) -> dict[str, Any]:
    triage = {"timezone": row.configuration.binding.timezone}
    if row.kind == ChangeRequestKind.PRODUCTION:
        return {
            **triage,
            "item_count": row.item_count,
            "period_start": row.period_start,
            "period_end": row.period_end,
            "incident_action": None,
            "incident_title": None,
        }
    change = row.incident_change
    if change.action in INCIDENT_EDITS:
        title, starts_at, ends_at = change.title, change.starts_at, change.ends_at
    else:
        title, starts_at = row.baseline["title"], _moment(row.baseline["starts_at"])
        ends_at = (
            change.ends_at
            if change.action == IncidentChangeAction.RESOLVE
            else _moment(row.baseline["ends_at"])
        )
    return {
        **triage,
        "item_count": 1,
        "period_start": starts_at,
        "period_end": ends_at,
        "incident_action": change.action,
        "incident_title": title,
    }


def _user(user: User | None) -> dict[str, Any] | None:
    return (
        {"id": user.pk, "name": user.get_full_name() or user.email or user.username}
        if user
        else None
    )


@transaction.atomic
def request_detail(actor: User, tenant_id: int, request_id: int) -> dict[str, Any]:
    request = _request(actor, tenant_id, request_id)
    plant = lock_incident_plant(tenant_id, request.plant.region_id, request.plant_id, lock=False)
    request = _request(actor, tenant_id, request_id)
    membership = tenant_membership(actor, tenant_id)
    own = request.proposed_by_id == actor.pk and request.proposer_membership_id == membership.pk
    if membership.role != WorkspaceRole.ADMIN and not own:
        raise ChangeRequest.DoesNotExist
    items = production_context(request) if request.kind == ChangeRequestKind.PRODUCTION else []
    conflict = None
    fingerprint = None
    if request.status == ChangeRequestStatus.PENDING:
        try:
            _validate(actor, request, plant, lock=False)
        except (ValidationError, ObjectDoesNotExist) as error:
            conflict = {
                "code": ErrorCode.REQUEST_CONFLICT.value,
                "message": " ".join(error.messages)
                if isinstance(error, ValidationError)
                else str(MESSAGES[ErrorCode.REQUEST_CONFLICT]),
            }
        fingerprint = "v1:" + digest(review_context(request, plant, production_items=items))
    incident = None
    if request.kind == ChangeRequestKind.INCIDENT:
        change = request.incident_change
        baseline = (
            IncidentRevision.objects.filter(
                incident_id=change.incident_id, revision=change.expected_revision
            ).first()
            if change.expected_revision is not None
            else None
        )
        incident = {
            "action": change.action,
            "incident_id": change.incident_id,
            "expected_revision": change.expected_revision,
            "values": incident_values(change) if change.action in INCIDENT_EDITS else None,
            "ends_at": change.ends_at if change.action == IncidentChangeAction.RESOLVE else None,
            "before": (
                {"id": baseline.incident_id, "revision": baseline.revision, **baseline.snapshot}
                if baseline
                else None
            ),
            "applied_revision_id": change.applied_revision_id,
        }
    former = not Membership.objects.filter(
        pk=request.proposer_membership_id,
        user_id=request.proposed_by_id,
        user__is_active=True,
        tenant_id=tenant_id,
    ).exists()
    return {
        "id": request.pk,
        "kind": request.kind,
        "status": request.status,
        "plant_id": plant.pk,
        "region_id": plant.region_id,
        "plant_name": plant.name,
        "configuration_id": request.configuration_id,
        "proposed_by": _user(request.proposed_by),
        "reason": request.reason,
        "submitted_at": request.submitted_at,
        "reviewed_by": _user(request.reviewed_by),
        "closed_at": request.closed_at,
        "decision_reason": request.decision_reason,
        "former_member": former,
        "can_review": membership.role == WorkspaceRole.ADMIN
        and request.status == ChangeRequestStatus.PENDING,
        "can_withdraw": own and request.status == ChangeRequestStatus.PENDING,
        "unread": own and request.status in DECIDED and request.outcome_seen_at is None,
        "conflict": conflict,
        "review_fingerprint": fingerprint,
        "production_items": items,
        "incident": incident,
        **_triage(_with_triage(ChangeRequest.objects.filter(pk=request.pk)).get()),
    }


def request_summaries(
    actor: User, tenant_id: int, rows: list[ChangeRequest]
) -> list[dict[str, Any]]:
    membership = tenant_membership(actor, tenant_id)
    if membership.role != WorkspaceRole.ADMIN:
        rows = [
            row
            for row in rows
            if row.proposed_by_id == actor.pk and row.proposer_membership_id == membership.pk
        ]
    active = set(
        Membership.objects.filter(
            pk__in=[row.proposer_membership_id for row in rows],
            user__is_active=True,
            tenant_id=tenant_id,
        ).values_list("pk", flat=True)
    )
    return [
        {
            "id": row.pk,
            "kind": row.kind,
            "status": row.status,
            "plant_id": row.plant_id,
            "region_id": row.plant.region_id,
            "plant_name": row.plant.name,
            "configuration_id": row.configuration_id,
            "proposed_by": _user(row.proposed_by),
            "reason": row.reason,
            "submitted_at": row.submitted_at,
            "reviewed_by": _user(row.reviewed_by),
            "closed_at": row.closed_at,
            "decision_reason": row.decision_reason,
            "former_member": row.proposer_membership_id not in active,
            "can_review": membership.role == WorkspaceRole.ADMIN
            and row.status == ChangeRequestStatus.PENDING,
            "can_withdraw": row.proposed_by_id == actor.pk
            and row.proposer_membership_id == membership.pk
            and row.status == ChangeRequestStatus.PENDING,
            "unread": row.proposed_by_id == actor.pk
            and row.proposer_membership_id == membership.pk
            and row.status in DECIDED
            and row.outcome_seen_at is None,
            **_triage(row),
        }
        for row in rows
    ]
