from collections.abc import Mapping
from typing import TYPE_CHECKING

from django.contrib.auth.models import User

from pyracast.event_details import INTEGER, DetailShape, shaped
from pyracast.regions.models import AuditAction, AuditEvent, Region

if TYPE_CHECKING:
    from pyracast.connections.models import ProviderAccount

BINDING: DetailShape = {"binding_id": INTEGER}
LOCATION_REQUEST: DetailShape = {"request_id": INTEGER}
AUDIT_DETAILS: Mapping[str, DetailShape] = {
    AuditAction.REGION_RENAMED: {},
    AuditAction.LOCATION_CHANGE_REQUESTED: LOCATION_REQUEST,
    AuditAction.LOCATION_CHANGE_APPROVED: LOCATION_REQUEST,
    AuditAction.LOCATION_CHANGE_REJECTED: LOCATION_REQUEST,
    AuditAction.LOCATION_CHANGE_WITHDRAWN: LOCATION_REQUEST,
    AuditAction.LOCATION_CHANGE_REBASED: {},
    AuditAction.BINDING_CONFIGURED: BINDING,
    AuditAction.CONNECTION_VALIDATED: BINDING,
    AuditAction.REGION_APPROVED: BINDING,
    AuditAction.REGION_REJECTED: {},
    AuditAction.REGION_SUSPENDED: {},
    AuditAction.REGION_TRASHED: {},
    AuditAction.REGION_RESTORED: {},
    AuditAction.COLLECTION_SCHEDULE_UPDATED: {},
    AuditAction.REMINDER_SCHEDULE_UPDATED: {"schedule_revision": INTEGER},
    AuditAction.HISTORICAL_REFERENCE_IMPORTED: {},
    AuditAction.REFERENCE_CAPACITY_NORMALIZED: {},
    AuditAction.PROVIDER_ACCOUNT_CREATED: {},
    AuditAction.PROVIDER_SECRET_ROTATED: {},
    AuditAction.PROVIDER_COOLDOWN_CLEARED: {},
}


def record_audit(
    action: str,
    *,
    actor: User,
    revision: int,
    region: Region | None = None,
    provider_account: ProviderAccount | None = None,
    details: Mapping[str, object] | None = None,
) -> AuditEvent:
    return AuditEvent.objects.create(
        actor=actor,
        region=region,
        provider_account=provider_account,
        action=action,
        revision=revision,
        details=shaped("audit", action, details or {}, AUDIT_DETAILS),
    )
