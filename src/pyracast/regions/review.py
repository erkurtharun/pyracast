from typing import TYPE_CHECKING

from django.db.models import OuterRef, QuerySet, Subquery
from django.db.models.fields.json import KT
from django.utils.translation import gettext_lazy

from pyracast.errors import ConflictError, ErrorCode
from pyracast.regions.models import AuditAction, AuditEvent, Region, RegionStatus

if TYPE_CHECKING:
    from pyracast.connections.models import SiteBinding

S = RegionStatus
TRANSITIONS: frozenset[tuple[RegionStatus, RegionStatus]] = frozenset(
    {
        (S.PENDING, S.APPROVED),
        (S.PENDING, S.REJECTED),
        (S.APPROVED, S.SUSPENDED),
        (S.SUSPENDED, S.APPROVED),
        (S.APPROVED, S.PENDING),
        (S.SUSPENDED, S.PENDING),
        (S.REJECTED, S.PENDING),
    }
)
DECISION_UNAVAILABLE = gettext_lazy(
    "This review decision is unavailable for the current review status."
)
STATUS_CHANGED = gettext_lazy(
    "The review status changed after this page opened. Reload the plant and review it again."
)
SETUP_CHANGED = gettext_lazy(
    "The plant changed after this page opened. Reload the plant and review it again."
)
REVIEW_DECISIONS = (
    AuditAction.REGION_APPROVED,
    AuditAction.REGION_REJECTED,
    AuditAction.REGION_SUSPENDED,
)


def with_review_outcome(regions: QuerySet[Region]) -> QuerySet[Region]:
    """Annotates `reviewed_at` and `review_reason` from the region's latest review decision;
    an approval and a decision recorded before reasons were stored carry no reason."""
    latest = AuditEvent.objects.filter(region=OuterRef("pk"), action__in=REVIEW_DECISIONS).order_by(
        "-created_at", "-pk"
    )
    return regions.annotate(
        reviewed_at=Subquery(latest.values("created_at")[:1]),
        review_reason=Subquery(latest.annotate(reason=KT("details__reason")).values("reason")[:1]),
    )


def require_seen_status(region: Region, status: str | None) -> None:
    if status is not None and region.status != status:
        raise ConflictError(ErrorCode.CONFLICT, STATUS_CHANGED)


def set_review_status(
    region: Region,
    target: RegionStatus,
    *,
    binding: SiteBinding | None = None,
    fields: tuple[str, ...] = (),
) -> None:
    """The only writer of `Region.status`; the caller holds the region row lock. A suspended
    plant keeps its approved binding, so its saved forecasts stay readable as stale."""
    if (region.status, target) not in TRANSITIONS:
        raise ConflictError(ErrorCode.CONFLICT, DECISION_UNAVAILABLE)
    region.status = target
    if target == RegionStatus.APPROVED:
        region.approved_binding = binding
    elif target == RegionStatus.PENDING:
        region.approved_binding = None
    region.save(update_fields=("status", "approved_binding", *fields))
