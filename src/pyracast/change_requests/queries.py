from bisect import bisect_left
from datetime import datetime, timedelta
from typing import Any

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from django.db.models import Count, Max, Min, OuterRef, QuerySet, Subquery

from pyracast.accounts.authorization import tenant_membership
from pyracast.accounts.models import Membership
from pyracast.change_requests.models import (
    INCIDENT_EDITS,
    ChangeRequest,
    ChangeRequestKind,
    ChangeRequestStatus,
    IncidentChange,
    IncidentChangeAction,
    ProductionChangeItem,
)
from pyracast.change_requests.operations import _validate
from pyracast.change_requests.policy import (
    can_review,
    can_withdraw,
    former_proposers,
    inbox_requests,
    is_unread,
    own_pending,
    request_for,
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

# Submission accepts only 30 or 60 minute production intervals.
LONGEST_PRODUCTION_ITEM = timedelta(hours=1)


def inbox(
    actor: User, tenant_id: int, *, scope: str = "pending", plant_id: int | None = None
) -> QuerySet[ChangeRequest]:
    rows = inbox_requests(tenant_membership(actor, tenant_id), scope)
    if plant_id is not None:
        rows = rows.filter(plant_id=plant_id)
    return _with_triage(rows).order_by("-created_at", "-pk")


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


def _summary(row: Any, membership: Membership, *, former: bool) -> dict[str, Any]:
    return {
        "id": row.pk,
        "kind": row.kind,
        "status": row.status,
        "plant_id": row.plant_id,
        "region_id": row.plant.region_id,
        "plant_name": row.plant.name,
        "configuration_id": row.configuration_id,
        "proposed_by": _user(row.proposed_by),
        "reason": row.reason,
        "created_at": row.created_at,
        "reviewed_by": _user(row.reviewed_by),
        "closed_at": row.closed_at,
        "decision_reason": row.decision_reason,
        "former_member": former,
        "can_review": can_review(row, membership),
        "can_withdraw": can_withdraw(row, membership),
        "unread": is_unread(row, membership),
        **_triage(row),
    }


def _single_summary(row: Any, membership: Membership) -> dict[str, Any]:
    former = former_proposers(membership.tenant_id, [row])
    return _summary(row, membership, former=row.pk in former)


def _summary_of(membership: Membership, request_id: int) -> dict[str, Any]:
    return _single_summary(
        _with_triage(ChangeRequest.objects.filter(pk=request_id)).get(), membership
    )


@transaction.atomic
def request_detail(actor: User, tenant_id: int, request_id: int) -> dict[str, Any]:
    membership = tenant_membership(actor, tenant_id)
    request = request_for(membership, request_id)
    plant = lock_incident_plant(tenant_id, request.plant.region_id, request.plant_id, lock=False)
    request = _with_triage(visible_requests(membership)).get(pk=request_id)
    items = production_context(request) if request.kind == ChangeRequestKind.PRODUCTION else []
    conflict = None
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
    fingerprint = (
        "v1:" + digest(review_context(request, plant, production_items=items))
        if can_review(request, membership)
        else None
    )
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
    return {
        **_single_summary(request, membership),
        "conflict": conflict,
        "review_fingerprint": fingerprint,
        "production_items": items,
        "incident": incident,
    }


def with_own_pending_requests(
    actor: User, tenant_id: int, plant_id: int, history: dict[str, Any]
) -> dict[str, Any]:
    """Each interval names the actor's latest pending production request with an item
    overlapping it. The pending quota bounds the items read to 20 requests of 100 items."""
    membership = tenant_membership(actor, tenant_id)
    items = sorted(
        ProductionChangeItem.objects.filter(
            request__in=own_pending(membership, plant_id)
        ).values_list("starts_at", "ends_at", "request_id")
    )
    starts = [start for start, _, _ in items]

    def latest(start: datetime, end: datetime) -> int | None:
        nearby = items[
            bisect_left(starts, start - LONGEST_PRODUCTION_ITEM) : bisect_left(starts, end)
        ]
        return max((request for _, finish, request in nearby if finish > start), default=None)

    return {
        **history,
        "intervals": [
            {**row, "pending_request_id": latest(row["starts_at"], row["ends_at"])}
            for row in history["intervals"]
        ],
    }


def own_pending_outages(
    actor: User, tenant_id: int, plant_id: int, incident_ids: list[int]
) -> dict[int, int]:
    """The actor's latest pending request per outage it targets."""
    return dict(
        IncidentChange.objects.filter(
            request__in=own_pending(tenant_membership(actor, tenant_id), plant_id),
            incident_id__in=incident_ids,
        )
        .order_by("request_id")
        .values_list("incident_id", "request_id")
    )


def request_summary(actor: User, tenant_id: int, request: ChangeRequest) -> dict[str, Any]:
    return _summary_of(tenant_membership(actor, tenant_id), request.pk)


def request_summaries(
    actor: User, tenant_id: int, rows: list[ChangeRequest]
) -> list[dict[str, Any]]:
    membership = tenant_membership(actor, tenant_id)
    former = former_proposers(tenant_id, rows)
    return [_summary(row, membership, former=row.pk in former) for row in rows]
