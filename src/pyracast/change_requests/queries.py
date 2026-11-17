from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from django.db.models import QuerySet

from pyracast.accounts.authorization import tenant_membership
from pyracast.accounts.models import Membership
from pyracast.change_requests.models import ChangeRequest
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
    rows = visible_requests(actor, tenant_id)
    if membership.role != Membership.Role.ADMIN or scope == "mine":
        rows = rows.filter(proposed_by=actor, proposer_membership_id=membership.pk)
    if scope == "pending":
        rows = rows.filter(status="pending")
    elif scope == "history":
        rows = rows.exclude(status="pending")
    if plant_id is not None:
        rows = rows.filter(plant_id=plant_id)
    return rows.select_related("plant__region", "proposed_by", "reviewed_by").order_by(
        "-submitted_at", "-pk"
    )


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
    if membership.role != Membership.Role.ADMIN and not own:
        raise ChangeRequest.DoesNotExist
    items = production_context(request) if request.kind == "production" else []
    conflict = None
    fingerprint = None
    if request.status == "pending":
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
    if request.kind == "incident":
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
            "values": incident_values(change) if change.action in {"create", "update"} else None,
            "ends_at": change.ends_at if change.action == "resolve" else None,
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
        "timezone": request.configuration.binding.timezone,
        "configuration_id": request.configuration_id,
        "proposed_by": _user(request.proposed_by),
        "reason": request.reason,
        "submitted_at": request.submitted_at,
        "reviewed_by": _user(request.reviewed_by),
        "closed_at": request.closed_at,
        "decision_reason": request.decision_reason,
        "former_member": former,
        "can_review": membership.role == Membership.Role.ADMIN and request.status == "pending",
        "can_withdraw": own and request.status == "pending",
        "unread": own
        and request.status in {"approved", "rejected"}
        and request.outcome_seen_at is None,
        "conflict": conflict,
        "review_fingerprint": fingerprint,
        "production_items": items,
        "incident": incident,
    }


def request_summaries(
    actor: User, tenant_id: int, rows: list[ChangeRequest]
) -> list[dict[str, Any]]:
    membership = tenant_membership(actor, tenant_id)
    if membership.role != Membership.Role.ADMIN:
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
            "can_review": membership.role == Membership.Role.ADMIN and row.status == "pending",
            "can_withdraw": row.proposed_by_id == actor.pk
            and row.proposer_membership_id == membership.pk
            and row.status == "pending",
            "unread": row.proposed_by_id == actor.pk
            and row.proposer_membership_id == membership.pk
            and row.status in {"approved", "rejected"}
            and row.outcome_seen_at is None,
        }
        for row in rows
    ]
