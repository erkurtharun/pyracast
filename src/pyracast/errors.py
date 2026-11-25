from collections.abc import Mapping
from typing import TYPE_CHECKING

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, models
from django.utils.translation import gettext_lazy as _
from psycopg.errors import DeadlockDetected, LockNotAvailable, QueryCanceled

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise, StrPromise


class ErrorCode(models.TextChoices):
    INVALID_REQUEST = "invalid_request"
    BAD_REQUEST = "bad_request"
    SESSION_ENDED = "session_ended"
    SESSION_CHANGED = "session_changed"
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
    CONFLICT = "conflict"
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
    MAINTENANCE = "maintenance"
    REQUEST_TOO_LARGE = "request_too_large"
    FORECAST_SOURCES_CHANGED = "forecast_sources_changed"
    CONFIGURATION_UNAVAILABLE = "configuration_unavailable"
    LOCATION_ALREADY_HAS_PLANT = "location_already_has_plant"
    PLANT_SUBMISSION_REQUIRED = "plant_submission_required"
    ESTIMATED_ACTUALS_UNAVAILABLE = "estimated_actuals_unavailable"
    LOCATION_UNCHANGED = "location_unchanged"
    LOCATION_ALREADY_EXISTS = "location_already_exists"
    CALCULATION_OUT_OF_RANGE = "calculation_out_of_range"
    TRASH_RETENTION_EXPIRED = "trash_retention_expired"
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
    PRODUCTION_IMPLAUSIBLE = "production_implausible"
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
    REMINDER_TIME_REQUIRED = "reminder_time_required"
    DUPLICATE_REMINDER_TIMES = "duplicate_reminder_times"
    TOO_MANY_REMINDER_TIMES = "too_many_reminder_times"
    RECIPIENT_REQUIRED = "recipient_required"
    DUPLICATE_RECIPIENTS = "duplicate_recipients"
    RECIPIENT_LIMIT_EXCEEDED = "recipient_limit_exceeded"
    REMINDER_SCHEDULE_CHANGED = "reminder_schedule_changed"
    REMINDER_DISCLOSURE_CHANGED = "reminder_disclosure_changed"
    PLANT_NOT_APPROVED = "plant_not_approved"
    PROVIDER_ACCOUNT_UNAVAILABLE = "provider_account_unavailable"
    CONFIGURATION_CHANGED = "configuration_changed"
    PRODUCTION_SOURCE_CHANGED = "production_source_changed"
    OUTAGE_HISTORY_CHANGED = "outage_history_changed"
    INCUMBENT_CHANGED = "incumbent_changed"
    CALIBRATION_NOT_ACTIVATABLE = "calibration_not_activatable"
    REVIEW_REASON_REQUIRED = "review_reason_required"
    PLANT_PARAMETERS_CHANGED = "plant_parameters_changed"
    PLANT_EDIT_BLOCKED = "plant_edit_blocked"


UNEXPECTED = _("Something went wrong. Try again.")
FORECAST_UPDATE_DELAYED = _("Forecast update delayed. Pyracast will try again.")
FUTURE_TIME = _("Choose now or an earlier time.")
TECHNICAL_DETAILS_CHANGED = _(
    "The plant's technical details changed. Reload the page and try again."
)
NO_NEW_FORECAST_DATA = _("No new forecast data is arriving for this plant.")
NOT_SAVED_NOW = _("This could not be saved right now. Try again in a moment.")
SIGN_IN_AGAIN = _("Sign in again.")
ENTER_A_VALUE = _("Enter a value.")

# No default for the two limit refusals: their raise sites must pass the text with the limit.
MESSAGES: Mapping[ErrorCode, StrPromise] = {
    ErrorCode.INVALID_REQUEST: _("Check the highlighted values and try again."),
    ErrorCode.BAD_REQUEST: UNEXPECTED,
    ErrorCode.SESSION_ENDED: SIGN_IN_AGAIN,
    ErrorCode.SESSION_CHANGED: SIGN_IN_AGAIN,
    ErrorCode.NOT_AUTHENTICATED: _("Sign in to continue."),
    ErrorCode.CSRF_FAILED: _(
        "This form is out of date. Copy what you typed, reload the page and try again."
    ),
    ErrorCode.PERMISSION_DENIED: _("You do not have permission for this action."),
    ErrorCode.INVALID_PROXY_HEADERS: UNEXPECTED,
    ErrorCode.MEMBERSHIP_REQUIRED: _("You are not a member of this workspace."),
    ErrorCode.WORKSPACE_ADMIN_REQUIRED: _("Only the workspace administrator can do this."),
    ErrorCode.ADMIN_EDITS_DIRECTLY: _("As the workspace administrator, make this change directly."),
    ErrorCode.PLATFORM_ADMIN_REQUIRED: _("Only Pyracast can do this."),
    ErrorCode.INVITATION_EMAIL_UNVERIFIED: _("Verify the invited email address before accepting."),
    ErrorCode.NOT_FOUND: _("The requested item was not found."),
    ErrorCode.METHOD_NOT_ALLOWED: UNEXPECTED,
    ErrorCode.NOT_ACCEPTABLE: UNEXPECTED,
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: UNEXPECTED,
    ErrorCode.CONFLICT: _(
        "Something changed in the meantime. Check the current values and try again."
    ),
    ErrorCode.THROTTLED: _("Too many attempts in a short time. Wait a moment and try again."),
    ErrorCode.SESSION_BUSY: _("This could not be done right now. Try again in a moment."),
    ErrorCode.INVITATION_RATE_LIMITED: _("Too many invitations were sent. Try again later."),
    ErrorCode.PROVIDER_THROTTLED: FORECAST_UPDATE_DELAYED,
    ErrorCode.QUOTA_EXHAUSTED: FORECAST_UPDATE_DELAYED,
    ErrorCode.SERVER_ERROR: UNEXPECTED,
    ErrorCode.PROVIDER_UNAVAILABLE: FORECAST_UPDATE_DELAYED,
    ErrorCode.BUSY: NOT_SAVED_NOW,
    ErrorCode.DATABASE_CONFLICT: NOT_SAVED_NOW,
    ErrorCode.REQUEST_TIMEOUT: _("This took too long. Try again later."),
    ErrorCode.PROVIDER_AUTHENTICATION_FAILED: NO_NEW_FORECAST_DATA,
    ErrorCode.MAINTENANCE: _(
        "Pyracast is being updated. Try again in a minute; your data is safe."
    ),
    ErrorCode.REQUEST_TOO_LARGE: UNEXPECTED,
    ErrorCode.FORECAST_SOURCES_CHANGED: _("The forecast data changed while loading. Try again."),
    ErrorCode.CONFIGURATION_UNAVAILABLE: _("This plant has no current technical details."),
    ErrorCode.LOCATION_ALREADY_HAS_PLANT: _(
        "This location already has an active plant. Open the existing plant."
    ),
    ErrorCode.PLANT_SUBMISSION_REQUIRED: _(
        "Complete the plant's technical details before sending it for approval."
    ),
    ErrorCode.ESTIMATED_ACTUALS_UNAVAILABLE: _("No satellite data for the selected time range."),
    ErrorCode.LOCATION_UNCHANGED: _("Enter a location different from the current location."),
    ErrorCode.LOCATION_ALREADY_EXISTS: _(
        "This workspace already has a plant at this location. Open the existing plant."
    ),
    ErrorCode.CALCULATION_OUT_OF_RANGE: _("The forecast for this day could not be calculated."),
    ErrorCode.TRASH_RETENTION_EXPIRED: _("The 30-day restore period has ended."),
    ErrorCode.DATE_OUT_OF_RANGE: _(
        "The selected data is not available for this date. Choose another date."
    ),
    ErrorCode.COMPLETED_DAY_REQUIRED: _("Choose today or an earlier day."),
    ErrorCode.INVALID_EPIAS_PLANT: _("Enter a positive EPİAŞ plant ID and the plant's EIC."),
    ErrorCode.EPIAS_PLANT_IDENTITY_MISMATCH: _(
        "The EPİAŞ plant ID and EIC did not match. Check both identifiers."
    ),
    ErrorCode.INCIDENT_CLOSED: _("This outage is no longer ongoing."),
    ErrorCode.INCIDENT_OVERLAP: _(
        "Another outage already covers part of this time. Change the start or the end."
    ),
    ErrorCode.INCIDENT_STARTS_IN_FUTURE: FUTURE_TIME,
    ErrorCode.INCIDENT_ENDS_BEFORE_START: _("The outage end must be after its start."),
    ErrorCode.INCIDENT_ENDS_IN_FUTURE: FUTURE_TIME,
    ErrorCode.INCIDENT_CAPACITY_EXCEEDED: _(
        "Enter a value no greater than the one in the plant's technical details."
    ),
    ErrorCode.PRODUCTION_IMPLAUSIBLE: _(
        "Some values exceed what the plant can produce. Check the unit, then confirm to save."
    ),
    ErrorCode.INVITATION_UNAVAILABLE: _(
        "This invitation is no longer valid. Ask the workspace administrator for a new one."
    ),
    ErrorCode.INVITATION_MEMBER_EXISTS: _("This account already belongs to the workspace."),
    ErrorCode.WORKSPACE_ADMIN_NOT_REMOVABLE: _("The workspace administrator cannot be removed."),
    ErrorCode.MEMBER_UNAVAILABLE: _("Choose an active member of this workspace."),
    ErrorCode.REGION_REVISION_CHANGED: _(
        "The plant's location changed in the meantime. Check the current location and try again."
    ),
    ErrorCode.LOCATION_CHANGE_ALREADY_PENDING: _(
        "A location change request is already in review by Pyracast."
    ),
    ErrorCode.LOCATION_CHANGE_ALREADY_REVIEWED: _(
        "This location change request was already approved, rejected or withdrawn. "
        "Reload the page to see it."
    ),
    ErrorCode.INCIDENT_REVISION_CHANGED: _(
        "The outage changed in the meantime. Check its current values and save again."
    ),
    ErrorCode.IDEMPOTENCY_KEY_REUSED: UNEXPECTED,
    ErrorCode.PENDING_REQUEST_LIMIT: _(
        "Too many of your change requests are waiting for approval. "
        "Withdraw one or wait for a decision before sending another."
    ),
    ErrorCode.PRODUCTION_REVISION_CHANGED: _(
        "These production values changed in the meantime. Check the current values and save again."
    ),
    ErrorCode.REQUEST_ALREADY_CLOSED: _(
        "This change request was already approved, rejected or withdrawn."
    ),
    ErrorCode.REQUEST_CONFLICT: _(
        "This change request no longer matches the plant and cannot be approved."
    ),
    ErrorCode.REVIEW_CONTEXT_CHANGED: _(
        "This change request or the plant changed after you opened it. "
        "Reload the page and check it again."
    ),
    ErrorCode.WORKSPACE_ADMIN_CHANGED: _(
        "The workspace administrator changed. Reload the page and try again."
    ),
    ErrorCode.REMINDER_TIME_REQUIRED: _(
        "Add at least one send time to turn on the forecast email."
    ),
    ErrorCode.DUPLICATE_REMINDER_TIMES: _("Use each send time once."),
    ErrorCode.RECIPIENT_REQUIRED: _("Add a recipient or turn on All members."),
    ErrorCode.DUPLICATE_RECIPIENTS: _("Use each address once."),
    ErrorCode.REMINDER_SCHEDULE_CHANGED: _(
        "The forecast email settings changed in the meantime. Check them and save again."
    ),
    ErrorCode.REMINDER_DISCLOSURE_CHANGED: _(
        "The recipients changed in the meantime. Check them and save again."
    ),
    ErrorCode.PLANT_NOT_APPROVED: _(
        "The forecast email can be turned on once Pyracast approves the plant."
    ),
    ErrorCode.PROVIDER_ACCOUNT_UNAVAILABLE: _(
        "No new forecast data is arriving for this plant. The forecast email cannot be turned on."
    ),
    ErrorCode.CONFIGURATION_CHANGED: TECHNICAL_DETAILS_CHANGED,
    ErrorCode.PRODUCTION_SOURCE_CHANGED: _(
        "The source of the actual production changed after this calibration was calculated. "
        "Calculate a new calibration."
    ),
    ErrorCode.OUTAGE_HISTORY_CHANGED: _(
        "Outages in its period changed after this calibration was calculated. "
        "Calculate a new calibration."
    ),
    ErrorCode.INCUMBENT_CHANGED: _(
        "The calibration in use changed after this one was calculated. Calculate a new calibration."
    ),
    ErrorCode.CALIBRATION_NOT_ACTIVATABLE: _(
        "Only a calculated calibration that was checked on test days can be used."
    ),
    ErrorCode.REVIEW_REASON_REQUIRED: _("Enter the reason the customer will read."),
    ErrorCode.PLANT_PARAMETERS_CHANGED: _(
        "The technical details changed after you opened them. Check the values and save again."
    ),
    ErrorCode.PLANT_EDIT_BLOCKED: _("The plant's technical details cannot be changed now."),
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
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(MESSAGES[code] if message is None else message, code=code)
        self.code: ErrorCode = code
        self.field = field
        self.details = details
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


_RETRYABLE_DATABASE_FAILURES: tuple[tuple[type[Exception], ErrorCode], ...] = (
    (LockNotAvailable, ErrorCode.BUSY),
    (DeadlockDetected, ErrorCode.DATABASE_CONFLICT),
    (QueryCanceled, ErrorCode.REQUEST_TIMEOUT),
)


def retryable_database_code(error: DatabaseError) -> ErrorCode | None:
    return next(
        (code for kind, code in _RETRYABLE_DATABASE_FAILURES if isinstance(error.__cause__, kind)),
        None,
    )
