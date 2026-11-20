from collections.abc import Iterable, Sequence

from django.contrib.auth.models import User
from django.db.models import Count, Q, QuerySet

from pyracast.accounts.models import Membership, WorkspaceRole
from pyracast.change_requests.models import DECIDED, ChangeRequest, ChangeRequestStatus
from pyracast.plants.models import Plant, active_plant

PENDING = Q(status=ChangeRequestStatus.PENDING)
PENDING_LIMIT = 20


def authored_by(membership: Membership) -> Q:
    """A request belongs to the membership that proposed it; a rejoined member is a new one."""
    return Q(proposed_by_id=membership.user_id, proposer_membership_id=membership.pk)


def _unread(user: User, memberships: Iterable[Membership]) -> Q:
    return Q(
        proposed_by_id=user.pk,
        proposer_membership_id__in=[membership.pk for membership in memberships],
        status__in=DECIDED,
        outcome_seen_at__isnull=True,
    )


def visible_requests(membership: Membership) -> QuerySet[ChangeRequest]:
    rows = ChangeRequest.objects.filter(
        active_plant("plant__"), plant__region__tenant_id=membership.tenant_id
    )
    if membership.role == WorkspaceRole.ADMIN:
        return rows
    return rows.filter(authored_by(membership))


def inbox_requests(membership: Membership, scope: str) -> QuerySet[ChangeRequest]:
    rows = visible_requests(membership)
    if scope == "mine":
        rows = rows.filter(authored_by(membership))
    if scope == "pending":
        return rows.filter(PENDING)
    if scope == "history":
        return rows.exclude(PENDING)
    return rows


def request_for(membership: Membership, request_id: int, *, lock: bool = False) -> ChangeRequest:
    rows = visible_requests(membership).select_related(
        "plant__region", "configuration__binding", "proposed_by", "reviewed_by"
    )
    if lock:
        rows = rows.select_for_update(of=("self",))
    return rows.get(pk=request_id)


def is_author(request: ChangeRequest, membership: Membership) -> bool:
    return (
        request.proposed_by_id == membership.user_id
        and request.proposer_membership_id == membership.pk
    )


def is_unread(request: ChangeRequest, membership: Membership) -> bool:
    return (
        is_author(request, membership)
        and request.status in DECIDED
        and request.outcome_seen_at is None
    )


def can_review(request: ChangeRequest, membership: Membership) -> bool:
    return membership.role == WorkspaceRole.ADMIN and request.status == ChangeRequestStatus.PENDING


def can_withdraw(request: ChangeRequest, membership: Membership) -> bool:
    return is_author(request, membership) and request.status == ChangeRequestStatus.PENDING


def pending_limit_reached(membership: Membership, plant: Plant) -> bool:
    return (
        ChangeRequest.objects.filter(authored_by(membership), PENDING, plant=plant).count()
        >= PENDING_LIMIT
    )


def former_proposers(tenant_id: int, requests: Iterable[ChangeRequest]) -> set[int]:
    """Request ids whose proposing membership is gone or belongs to a deactivated account."""
    rows = list(requests)
    active = set(
        Membership.objects.filter(
            pk__in=[row.proposer_membership_id for row in rows],
            user__is_active=True,
            tenant_id=tenant_id,
        ).values_list("pk", flat=True)
    )
    return {row.pk for row in rows if row.proposer_membership_id not in active}


def workspace_counters(
    user: User, memberships: Sequence[Membership]
) -> dict[int, tuple[int | None, int]]:
    """Per workspace: requests awaiting review (administrators only) and unseen outcomes."""
    totals = {
        row["plant__region__tenant_id"]: row
        for row in ChangeRequest.objects.filter(
            active_plant("plant__"),
            plant__region__tenant_id__in=[membership.tenant_id for membership in memberships],
        )
        .values("plant__region__tenant_id")
        .annotate(
            pending=Count("pk", filter=PENDING),
            unread=Count("pk", filter=_unread(user, memberships)),
        )
    }
    counters: dict[int, tuple[int | None, int]] = {}
    for membership in memberships:
        row = totals.get(membership.tenant_id, {"pending": 0, "unread": 0})
        pending = row["pending"] if membership.role == WorkspaceRole.ADMIN else None
        counters[membership.tenant_id] = (pending, row["unread"])
    return counters
