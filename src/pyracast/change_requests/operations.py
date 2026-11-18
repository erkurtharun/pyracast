from dataclasses import asdict
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from pyracast.accounts.authorization import tenant_membership
from pyracast.accounts.models import Membership, WorkspaceRole
from pyracast.change_requests.models import (
    DECIDED,
    INCIDENT_EDITS,
    ChangeRequest,
    ChangeRequestKind,
    ChangeRequestStatus,
    IncidentChange,
    IncidentChangeAction,
    ProductionChangeAction,
    ProductionChangeItem,
)
from pyracast.change_requests.preparation import (
    INCIDENT_FIELDS,
    decision_context,
    digest,
    incident_values,
    prepare_incident_change,
    production_changes,
    production_context,
    validate_request,
)
from pyracast.errors import ConflictError, DomainError, ErrorCode
from pyracast.incidents.models import IncidentRevision
from pyracast.incidents.operations import (
    finish_incident,
    lock_incident_plant,
    save_incident,
)
from pyracast.plants.models import Plant
from pyracast.plants.production_changes import (
    ProductionChange,
    change_production,
    lock_production_configuration,
    prepare_production_changes,
)


def _reason(reason: str, *, required: bool = False) -> str:
    if (
        not isinstance(reason, str)
        or len(reason.strip()) > 500
        or (required and not reason.strip())
    ):
        raise DomainError(
            ErrorCode.INVALID_REQUEST,
            _("A reason of 1 to 500 characters is required."),
            field="reason",
        )
    return reason.strip()


def _replay(membership: Membership, key: UUID, payload_hash: str) -> ChangeRequest | None:
    previous = ChangeRequest.objects.filter(
        proposer_membership_id=membership.pk, idempotency_key=key
    ).first()
    if previous is not None and previous.payload_sha256 != payload_hash:
        raise ConflictError(ErrorCode.IDEMPOTENCY_KEY_REUSED)
    return previous


def _create(
    membership: Membership,
    plant: Plant,
    configuration_id: int,
    kind: str,
    reason: str,
    key: UUID,
    payload_hash: str,
) -> ChangeRequest:
    if (
        ChangeRequest.objects.filter(
            proposer_membership_id=membership.pk, plant=plant, status=ChangeRequestStatus.PENDING
        ).count()
        >= 20
    ):
        raise ConflictError(ErrorCode.PENDING_REQUEST_LIMIT)
    return ChangeRequest.objects.create(
        plant=plant,
        configuration_id=configuration_id,
        kind=kind,
        proposed_by=membership.user,
        proposer_membership_id=membership.pk,
        reason=reason,
        idempotency_key=key,
        payload_sha256=payload_hash,
    )


@transaction.atomic
def submit_production(
    actor: User,
    tenant_id: int,
    region_id: int,
    plant_id: int,
    *,
    configuration_id: int,
    unit: str,
    changes: list[ProductionChange],
    reason: str,
    idempotency_key: UUID,
) -> ChangeRequest:
    membership = tenant_membership(actor, tenant_id, role=WorkspaceRole.VIEWER, lock=True)
    reason = _reason(reason, required=True)
    if not 1 <= len(changes) <= 100:
        raise DomainError(
            ErrorCode.INVALID_REQUEST,
            _("Submit 1 to 100 production intervals."),
            field="changes",
        )
    payload_hash = digest(
        {
            "plant": plant_id,
            "region": region_id,
            "configuration": configuration_id,
            "unit": unit,
            "changes": [asdict(row) for row in changes],
            "reason": reason,
        }
    )
    previous = _replay(membership, idempotency_key, payload_hash)
    if previous is not None:
        return previous
    configuration = lock_production_configuration(tenant_id, region_id, plant_id, configuration_id)
    prepared = prepare_production_changes(configuration, unit, changes)
    request = _create(
        membership,
        configuration.plant,
        configuration_id,
        ChangeRequestKind.PRODUCTION,
        reason,
        idempotency_key,
        payload_hash,
    )
    for row in prepared:
        ProductionChangeItem.objects.create(
            request=request,
            starts_at=row.starts_at,
            ends_at=row.ends_at,
            action=ProductionChangeAction.REVERT if row.reverted else ProductionChangeAction.SET,
            energy_mwh=row.energy_mwh,
            quality=row.quality,
            source=row.source,
            expected_head=row.previous,
        )
    for context in production_context(request):
        current = context["current"]
        ProductionChangeItem.objects.filter(pk=context["id"]).update(
            baseline_energy_mwh=current["energy_mwh"] if current else None,
            baseline_source_kind=current["source_kind"] if current else "",
        )
    return request


@transaction.atomic
def submit_incident(
    actor: User,
    tenant_id: int,
    region_id: int,
    plant_id: int,
    *,
    action: str,
    idempotency_key: UUID,
    reason: str = "",
    incident_id: int | None = None,
    expected_revision: int | None = None,
    values: dict[str, Any] | None = None,
    ends_at: datetime | None = None,
) -> ChangeRequest:
    membership = tenant_membership(actor, tenant_id, role=WorkspaceRole.VIEWER, lock=True)
    reason = _reason(reason)
    payload_hash = digest(
        {
            "plant": plant_id,
            "region": region_id,
            "action": action,
            "incident": incident_id,
            "expected_revision": expected_revision,
            "values": values,
            "ends_at": ends_at,
            "reason": reason,
        }
    )
    previous = _replay(membership, idempotency_key, payload_hash)
    if previous is not None:
        return previous
    plant = lock_incident_plant(tenant_id, region_id, plant_id)
    prepared = prepare_incident_change(
        actor,
        plant,
        action=action,
        incident_id=incident_id,
        expected_revision=expected_revision,
        values=values,
        ends_at=ends_at,
    )
    request = _create(
        membership,
        plant,
        prepared.declared_configuration_id,
        ChangeRequestKind.INCIDENT,
        reason,
        idempotency_key,
        payload_hash,
    )
    IncidentChange.objects.create(
        request=request,
        action=action,
        incident_id=incident_id,
        expected_revision=expected_revision,
        **(
            {field: getattr(prepared, field) for field in INCIDENT_FIELDS}
            if action in INCIDENT_EDITS
            else {"ends_at": ends_at}
        ),
    )
    return request


def visible_requests(actor: User, membership: Membership) -> QuerySet[ChangeRequest]:
    tenant_id = membership.tenant_id
    return ChangeRequest.objects.filter(
        Q(plant__region__tenant__membership__role=WorkspaceRole.ADMIN)
        | Q(proposed_by_id=actor.pk, proposer_membership_id=membership.pk),
        plant__region__tenant_id=tenant_id,
        plant__region__tenant__membership__pk=membership.pk,
        plant__region__tenant__membership__user__is_active=True,
        plant__region__deleted_at__isnull=True,
    )


def _request(actor: User, tenant_id: int, request_id: int, *, lock: bool = False) -> ChangeRequest:
    rows = visible_requests(actor, tenant_membership(actor, tenant_id)).select_related(
        "plant__region", "configuration__binding", "proposed_by", "reviewed_by"
    )
    if lock:
        rows = rows.select_for_update(of=("self",))
    return rows.get(pk=request_id)


def _terminal(request: ChangeRequest, status: str) -> bool:
    if request.status == status:
        return True
    if request.status != ChangeRequestStatus.PENDING:
        raise ConflictError(ErrorCode.REQUEST_ALREADY_CLOSED)
    return False


def _validate(actor: User, request: ChangeRequest, plant: Plant, *, lock: bool = True) -> None:
    try:
        validate_request(actor, request, plant, lock=lock)
    except ValidationError as error:
        raise ConflictError(ErrorCode.REQUEST_CONFLICT, " ".join(error.messages)) from error


@transaction.atomic
def approve_request(
    actor: User, tenant_id: int, request_id: int, *, review_fingerprint: str
) -> ChangeRequest:
    tenant_membership(actor, tenant_id, role=WorkspaceRole.ADMIN, lock=True)
    scoped = _request(actor, tenant_id, request_id)
    plant = lock_incident_plant(tenant_id, scoped.plant.region_id, scoped.plant_id)
    request = _request(actor, tenant_id, request_id, lock=True)
    if _terminal(request, ChangeRequestStatus.APPROVED):
        return request
    _validate(actor, request, plant)
    context = decision_context(request, plant)
    if review_fingerprint != "v1:" + digest(context["context"]):
        raise ConflictError(ErrorCode.REVIEW_CONTEXT_CHANGED)
    request.reviewed_context = context
    request.save(update_fields=("reviewed_context",))
    scope = actor, tenant_id, plant.region_id, plant.pk
    if request.kind == ChangeRequestKind.PRODUCTION:
        result = change_production(
            *scope,
            configuration_id=request.configuration_id,
            unit="MWh",
            changes=production_changes(request),
        )
        for item, observation_id in zip(
            request.production_items.order_by("starts_at", "pk"), result["ids"], strict=True
        ):
            item.applied_observation_id = observation_id
            item.save(update_fields=("applied_observation",))
    else:
        change = request.incident_change
        if change.action in INCIDENT_EDITS:
            incident = save_incident(
                *scope,
                incident_id=change.incident_id,
                expected_revision=change.expected_revision,
                **incident_values(change),
            )
        else:
            incident = finish_incident(
                *scope,
                incident_id=cast(int, change.incident_id),
                expected_revision=cast(int, change.expected_revision),
                ends_at=change.ends_at,
                cancel=change.action == IncidentChangeAction.CANCEL,
            )
        change.applied_revision = IncidentRevision.objects.get(
            incident=incident, revision=incident.revision
        )
        change.save(update_fields=("applied_revision",))
    _close(request, ChangeRequestStatus.APPROVED, actor)
    return request


def _close(request: ChangeRequest, status: str, actor: User | None, reason: str = "") -> None:
    request.status, request.reviewed_by, request.decision_reason = status, actor, reason
    request.closed_at = timezone.now()
    if actor is not None and actor.pk == request.proposed_by_id:
        request.outcome_seen_at = request.closed_at
    request.save(
        update_fields=("status", "reviewed_by", "closed_at", "decision_reason", "outcome_seen_at")
    )


@transaction.atomic
def reject_request(actor: User, tenant_id: int, request_id: int, *, reason: str) -> ChangeRequest:
    tenant_membership(actor, tenant_id, role=WorkspaceRole.ADMIN, lock=True)
    scoped = _request(actor, tenant_id, request_id)
    plant = lock_incident_plant(tenant_id, scoped.plant.region_id, scoped.plant_id)
    request = _request(actor, tenant_id, request_id, lock=True)
    if not _terminal(request, ChangeRequestStatus.REJECTED):
        reason = _reason(reason, required=True)
        request.reviewed_context = decision_context(request, plant)
        request.save(update_fields=("reviewed_context",))
        _close(request, ChangeRequestStatus.REJECTED, actor, reason)
    return request


@transaction.atomic
def withdraw_request(actor: User, tenant_id: int, request_id: int) -> ChangeRequest:
    membership = tenant_membership(actor, tenant_id, lock=True)
    request = _request(actor, tenant_id, request_id, lock=True)
    if request.proposed_by_id != actor.pk or request.proposer_membership_id != membership.pk:
        raise ChangeRequest.DoesNotExist
    if not _terminal(request, ChangeRequestStatus.WITHDRAWN):
        _close(request, ChangeRequestStatus.WITHDRAWN, None)
    return request


@transaction.atomic
def mark_seen(actor: User, tenant_id: int, request_id: int) -> ChangeRequest:
    membership = tenant_membership(actor, tenant_id, lock=True)
    request = _request(actor, tenant_id, request_id, lock=True)
    if request.proposed_by_id != actor.pk or request.proposer_membership_id != membership.pk:
        raise ChangeRequest.DoesNotExist
    if request.status in DECIDED and request.outcome_seen_at is None:
        request.outcome_seen_at = timezone.now()
        request.save(update_fields=("outcome_seen_at",))
    return request
