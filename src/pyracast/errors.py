from collections.abc import Mapping
from enum import StrEnum
from typing import TYPE_CHECKING

from django.core.exceptions import PermissionDenied, ValidationError
from django.utils.translation import gettext_lazy as _

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise, StrPromise


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    BAD_REQUEST = "bad_request"
    SESSION_ENDED = "session_ended"
    NOT_AUTHENTICATED = "not_authenticated"
    CSRF_FAILED = "csrf_failed"
    PERMISSION_DENIED = "permission_denied"
    INVALID_PROXY_HEADERS = "invalid_proxy_headers"
    MEMBERSHIP_REQUIRED = "membership_required"
    WORKSPACE_ADMIN_REQUIRED = "workspace_admin_required"
    ADMIN_EDITS_DIRECTLY = "admin_edits_directly"
    PLATFORM_ADMIN_REQUIRED = "platform_admin_required"
    INVITATION_EMAIL_UNVERIFIED = "invitation_email_unverified"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    NOT_ACCEPTABLE = "not_acceptable"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    THROTTLED = "throttled"
    SESSION_BUSY = "session_busy"
    INVITATION_RATE_LIMITED = "invitation_rate_limited"
    PROVIDER_THROTTLED = "provider_throttled"
    QUOTA_EXHAUSTED = "quota_exhausted"
    SERVER_ERROR = "server_error"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    BUSY = "busy"
    DATABASE_CONFLICT = "database_conflict"
    REQUEST_TIMEOUT = "request_timeout"
    PROVIDER_AUTHENTICATION_FAILED = "provider_authentication_failed"
    FORECAST_SOURCES_CHANGED = "forecast_sources_changed"
    CONFIGURATION_UNAVAILABLE = "configuration_unavailable"
    LOCATION_ALREADY_HAS_PLANT = "location_already_has_plant"
    PLANT_SUBMISSION_REQUIRED = "plant_submission_required"
    ESTIMATED_ACTUALS_UNAVAILABLE = "estimated_actuals_unavailable"
    LOCATION_UNCHANGED = "location_unchanged"
    LOCATION_ALREADY_EXISTS = "location_already_exists"
    CALCULATION_OUT_OF_RANGE = "calculation_out_of_range"
    TRASH_RETENTION_EXPIRED = "trash_retention_expired"
    PARENT_REGION_TRASHED = "parent_region_trashed"
    DATE_OUT_OF_RANGE = "date_out_of_range"
    COMPLETED_DAY_REQUIRED = "completed_day_required"
    INVALID_EPIAS_PLANT = "invalid_epias_plant"
    EPIAS_PLANT_IDENTITY_MISMATCH = "epias_plant_identity_mismatch"
    INCIDENT_CLOSED = "incident_closed"
    INCIDENT_OVERLAP = "incident_overlap"
    INCIDENT_STARTS_IN_FUTURE = "incident_starts_in_future"
    INCIDENT_ENDS_BEFORE_START = "incident_ends_before_start"
    INCIDENT_ENDS_IN_FUTURE = "incident_ends_in_future"
    INCIDENT_CAPACITY_EXCEEDED = "incident_capacity_exceeded"
    INVITATION_UNAVAILABLE = "invitation_unavailable"
    INVITATION_MEMBER_EXISTS = "invitation_member_exists"
    WORKSPACE_ADMIN_NOT_REMOVABLE = "workspace_admin_not_removable"
    MEMBER_UNAVAILABLE = "member_unavailable"
    REGION_REVISION_CHANGED = "region_revision_changed"
    LOCATION_CHANGE_ALREADY_PENDING = "location_change_already_pending"
    LOCATION_CHANGE_ALREADY_REVIEWED = "location_change_already_reviewed"
    INCIDENT_REVISION_CHANGED = "incident_revision_changed"
    IDEMPOTENCY_KEY_REUSED = "idempotency_key_reused"
    PENDING_REQUEST_LIMIT = "pending_request_limit"
    PRODUCTION_REVISION_CHANGED = "production_revision_changed"
    REQUEST_ALREADY_CLOSED = "request_already_closed"
    REQUEST_CONFLICT = "request_conflict"
    REVIEW_CONTEXT_CHANGED = "review_context_changed"
    WORKSPACE_ADMIN_CHANGED = "workspace_admin_changed"


MESSAGES: Mapping[ErrorCode, StrPromise] = {
    ErrorCode.INVALID_REQUEST: _("Check the highlighted values and try again."),
    ErrorCode.BAD_REQUEST: _("The request could not be processed."),
    ErrorCode.SESSION_ENDED: _("Your session ended. Sign in again."),
    ErrorCode.NOT_AUTHENTICATED: _("Sign in to continue."),
    ErrorCode.CSRF_FAILED: _("The page expired. Reload it and try again."),
    ErrorCode.PERMISSION_DENIED: _("You do not have permission for this action."),
    ErrorCode.INVALID_PROXY_HEADERS: _("The request could not be verified."),
    ErrorCode.MEMBERSHIP_REQUIRED: _("You are not a member of this workspace."),
    ErrorCode.WORKSPACE_ADMIN_REQUIRED: _("Workspace administrator required."),
    ErrorCode.ADMIN_EDITS_DIRECTLY: _(
        "Workspace members submit requests; administrators edit directly."
    ),
    ErrorCode.PLATFORM_ADMIN_REQUIRED: _("Platform administrator required."),
    ErrorCode.INVITATION_EMAIL_UNVERIFIED: _("Verify the invited email address before accepting."),
    ErrorCode.NOT_FOUND: _("The requested item was not found."),
    ErrorCode.METHOD_NOT_ALLOWED: _("This action is not available here."),
    ErrorCode.NOT_ACCEPTABLE: _("The requested response format is not available."),
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: _("Send the request as JSON."),
    ErrorCode.THROTTLED: _("Too many requests. Wait a moment and try again."),
    ErrorCode.SESSION_BUSY: _("Another request from this session is still running. Try again."),
    ErrorCode.INVITATION_RATE_LIMITED: _(
        "Too many invitations were sent. Wait a minute and try again."
    ),
    ErrorCode.PROVIDER_THROTTLED: _("The forecast provider is limiting requests. Try again later."),
    ErrorCode.QUOTA_EXHAUSTED: _(
        "The forecast provider's daily quota is used up. Try again tomorrow."
    ),
    ErrorCode.SERVER_ERROR: _("The server could not complete this request."),
    ErrorCode.PROVIDER_UNAVAILABLE: _("The forecast provider is unavailable. Try again later."),
    ErrorCode.BUSY: _("The workspace is busy. Try again in a moment."),
    ErrorCode.DATABASE_CONFLICT: _("The change collided with another change. Try again."),
    ErrorCode.REQUEST_TIMEOUT: _("The request took too long. Try again later."),
    ErrorCode.PROVIDER_AUTHENTICATION_FAILED: _(
        "The forecast provider rejected the stored credentials. Contact your administrator."
    ),
    ErrorCode.FORECAST_SOURCES_CHANGED: _("The forecast sources changed while loading. Try again."),
    ErrorCode.CONFIGURATION_UNAVAILABLE: _(
        "Plant configuration is unavailable. Complete setup and wait for administrator approval."
    ),
    ErrorCode.LOCATION_ALREADY_HAS_PLANT: _(
        "This location already has an active plant. Open the existing plant."
    ),
    ErrorCode.PLANT_SUBMISSION_REQUIRED: _(
        "Complete the plant's technical details before requesting approval."
    ),
    ErrorCode.ESTIMATED_ACTUALS_UNAVAILABLE: _(
        "Matching historical estimates are unavailable. Choose a day with collected data."
    ),
    ErrorCode.LOCATION_UNCHANGED: _("Enter a location different from the current location."),
    ErrorCode.LOCATION_ALREADY_EXISTS: _(
        "This location is already in your company's portfolio. Open the existing plant."
    ),
    ErrorCode.CALCULATION_OUT_OF_RANGE: _(
        "This forecast could not be calculated. Choose another day or contact your administrator."
    ),
    ErrorCode.TRASH_RETENTION_EXPIRED: _("The 30-day restore period has ended."),
    ErrorCode.PARENT_REGION_TRASHED: _("Restore this plant's location before restoring the plant."),
    ErrorCode.DATE_OUT_OF_RANGE: _("Choose a date within the available forecast range."),
    ErrorCode.COMPLETED_DAY_REQUIRED: _("Choose a completed local day for historical estimates."),
    ErrorCode.INVALID_EPIAS_PLANT: _("Enter a positive EPİAŞ plant ID and the plant's EIC."),
    ErrorCode.EPIAS_PLANT_IDENTITY_MISMATCH: _(
        "The EPİAŞ plant ID and EIC did not match. Check both identifiers."
    ),
    ErrorCode.INCIDENT_CLOSED: _("This outage is already closed."),
    ErrorCode.INCIDENT_OVERLAP: _("Outage periods must not overlap."),
    ErrorCode.INCIDENT_STARTS_IN_FUTURE: _("The outage start must not be in the future."),
    ErrorCode.INCIDENT_ENDS_BEFORE_START: _("The outage end must be after its start."),
    ErrorCode.INCIDENT_ENDS_IN_FUTURE: _("The outage end must not be in the future."),
    ErrorCode.INCIDENT_CAPACITY_EXCEEDED: _(
        "Available capacity must not exceed the configured capacity."
    ),
    ErrorCode.INVITATION_UNAVAILABLE: _("The invitation is invalid or expired."),
    ErrorCode.INVITATION_MEMBER_EXISTS: _("This account already belongs to the workspace."),
    ErrorCode.WORKSPACE_ADMIN_NOT_REMOVABLE: _("The workspace administrator cannot be removed."),
    ErrorCode.MEMBER_UNAVAILABLE: _("Choose an active member of this workspace."),
    ErrorCode.REGION_REVISION_CHANGED: _(
        "The plant location changed. Reopen the plant and try again."
    ),
    ErrorCode.LOCATION_CHANGE_ALREADY_PENDING: _(
        "Another location request is pending. Reopen the editor to see it."
    ),
    ErrorCode.LOCATION_CHANGE_ALREADY_REVIEWED: _(
        "This location request has already been reviewed. Refresh the page."
    ),
    ErrorCode.INCIDENT_REVISION_CHANGED: _("The outage changed. Reload the list and try again."),
    ErrorCode.IDEMPOTENCY_KEY_REUSED: _("Use a new request key for changed input."),
    ErrorCode.PENDING_REQUEST_LIMIT: _(
        "Resolve or withdraw pending requests before submitting more."
    ),
    ErrorCode.PRODUCTION_REVISION_CHANGED: _(
        "Production changed. Reload the latest values before saving."
    ),
    ErrorCode.REQUEST_ALREADY_CLOSED: _("The request already has a different outcome."),
    ErrorCode.REQUEST_CONFLICT: _("This request no longer matches the plant. Review it again."),
    ErrorCode.REVIEW_CONTEXT_CHANGED: _("The review context changed. Refresh and review again."),
    ErrorCode.WORKSPACE_ADMIN_CHANGED: _(
        "The workspace administrator changed. Reload before saving."
    ),
}


class DomainError(ValidationError):
    status = 400
    retry_after: int | None = None

    def __init__(
        self,
        code: ErrorCode,
        message: StrOrPromise | None = None,
        *,
        field: str | None = None,
        status: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(MESSAGES[code] if message is None else message, code=code)
        self.code: ErrorCode = code
        self.field = field
        if status is not None:
            self.status = status
        if retry_after is not None:
            self.retry_after = retry_after


class ConflictError(DomainError):
    status = 409


class BusyError(DomainError):
    status = 503
    retry_after = 1


class ForbiddenError(PermissionDenied):
    status = 403

    def __init__(self, code: ErrorCode) -> None:
        super().__init__(MESSAGES[code])
        self.code = code
