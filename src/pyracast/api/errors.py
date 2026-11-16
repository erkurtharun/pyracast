import math
from typing import Any

from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import OperationalError
from django.utils import timezone
from django.utils.translation import gettext, gettext_noop
from psycopg.errors import LockNotAvailable
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

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
    "parent_region_trashed": gettext_noop(
        "Restore this plant's location before restoring the plant."
    ),
    "date_out_of_range": gettext_noop("Choose a date within the available forecast range."),
    "completed_day_required": gettext_noop(
        "Choose a completed local day for historical estimates."
    ),
}


def _localized_message(message: str) -> str:
    return gettext(VALIDATION_DETAILS.get(message, message))


def exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    if isinstance(exc, OperationalError) and isinstance(exc.__cause__, LockNotAvailable):
        return Response({"code": "workspace_busy"}, status=503, headers={"Retry-After": "1"})
    if isinstance(exc, ObjectDoesNotExist):
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
    response = drf_exception_handler(exc, context)
    if response is not None:
        response.data = {"code": f"http_{response.status_code}", "detail": response.data}
    return response
