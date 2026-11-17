import logging
import math
from typing import Any
from uuid import uuid4

from django.core.exceptions import BadRequest, ObjectDoesNotExist, SuspiciousOperation
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import DatabaseError
from django.http import HttpRequest
from django.utils import timezone
from django.utils.translation import gettext, gettext_noop
from psycopg.errors import DeadlockDetected, LockNotAvailable, QueryCanceled
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler
from rest_framework.views import set_rollback

from pyracast.connections.contracts import ProviderError
from pyracast.connections.operations import QuotaExceededError, RequestDeferredError

VALIDATION_DETAILS = {
    "configuration_unavailable": gettext_noop(
        "Plant configuration is unavailable. Complete setup and wait for administrator approval."
    ),
    "location_already_has_plant": gettext_noop(
        "This location already has an active plant. Open the existing plant."
    ),
    "plant_submission_required": gettext_noop(
        "Complete the plant's technical details before requesting approval."
    ),
    "estimated_actuals_unavailable": gettext_noop(
        "Matching historical estimates are unavailable. Choose a day with collected data."
    ),
    "region_revision_changed": gettext_noop(
        "The plant location changed. Reopen the plant and try again."
    ),
    "location_change_already_pending": gettext_noop(
        "Another location request is pending. Reopen the editor to see it."
    ),
    "location_unchanged": gettext_noop("Enter a location different from the current location."),
    "location_change_already_reviewed": gettext_noop(
        "This location request has already been reviewed. Refresh the page."
    ),
    "location_already_exists": gettext_noop(
        "This location is already in your company's portfolio. Open the existing plant."
    ),
    "calculation_out_of_range": gettext_noop(
        "This forecast could not be calculated. Choose another day or contact your administrator."
    ),
    "trash_retention_expired": gettext_noop("The 30-day restore period has ended."),
    "date_out_of_range": gettext_noop("Choose a date within the available forecast range."),
    "completed_day_required": gettext_noop(
        "Choose a completed local day for historical estimates."
    ),
}


logger = logging.getLogger("pyracast.api")
SERVER_ERROR_MESSAGE = gettext_noop("The server could not complete this request.")


def _localized_message(message: str) -> str:
    return gettext(VALIDATION_DETAILS.get(message, message))


def log_server_error(
    request: HttpRequest | None,
    view: str,
    error: BaseException | None,
    *,
    code: str = "server_error",
) -> str:
    request_id = uuid4().hex
    # The path is client controlled: escape it so it cannot forge further log lines.
    path = getattr(request, "path", "-").encode("unicode_escape").decode("ascii")
    logger.error(
        "api_%s request_id=%s method=%s path=%s view=%s",
        code,
        request_id,
        getattr(request, "method", "-"),
        path,
        view,
        exc_info=error,
    )
    return request_id


def server_error_body(request_id: str) -> dict[str, str]:
    return {
        "code": "server_error",
        "message": gettext(SERVER_ERROR_MESSAGE),
        "request_id": request_id,
    }


def _database_response(exc: DatabaseError, context: dict[str, Any]) -> Response | None:
    if isinstance(exc.__cause__, LockNotAvailable):
        return Response({"code": "workspace_busy"}, status=503, headers={"Retry-After": "1"})
    if isinstance(exc.__cause__, DeadlockDetected):
        return Response({"code": "database_conflict"}, status=503, headers={"Retry-After": "1"})
    if isinstance(exc.__cause__, QueryCanceled):
        request_id = log_server_error(
            context.get("request"), _view_name(context), exc, code="request_timeout"
        )
        return Response({"code": "request_timeout", "request_id": request_id}, status=503)
    return None


def _view_name(context: dict[str, Any]) -> str:
    view = context.get("view")
    return "-" if view is None else type(view).__name__


def exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    if (
        isinstance(exc, DatabaseError)
        and (response := _database_response(exc, context)) is not None
    ):
        return response
    if isinstance(exc, ObjectDoesNotExist):
        logger.warning(
            "api_object_not_found model=%s", type(exc).__qualname__.removesuffix(".DoesNotExist")
        )
        return Response({"code": "not_found"}, status=404)
    if isinstance(exc, DjangoValidationError):
        detail = (
            {
                field: [_localized_message(message) for message in messages]
                for field, messages in exc.message_dict.items()
            }
            if hasattr(exc, "error_dict")
            else [_localized_message(message) for message in exc.messages]
        )
        return Response({"code": "invalid_request", "detail": detail}, status=400)
    if isinstance(exc, RequestDeferredError):
        if exc.available_at is None:
            return Response({"code": "provider_authentication_failed"}, status=503)
        return Response(
            {"code": "provider_throttled"},
            status=429,
            headers={
                "Retry-After": str(
                    max(0, math.ceil((exc.available_at - timezone.now()).total_seconds()))
                )
            },
        )
    if isinstance(exc, QuotaExceededError):
        return Response({"code": "quota_exhausted"}, status=429)
    if isinstance(exc, ProviderError):
        return Response({"code": "provider_unavailable"}, status=502)
    if isinstance(exc, SuspiciousOperation | BadRequest):
        # Django answers these through handler400 and logs them on django.security.
        return None
    response = drf_exception_handler(exc, context)
    if response is not None:
        response.data = {"code": f"http_{response.status_code}", "detail": response.data}
        return response
    set_rollback()
    request_id = log_server_error(context.get("request"), _view_name(context), exc)
    return Response(server_error_body(request_id), status=500)
