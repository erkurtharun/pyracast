import math
from typing import Any

from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from pyracast.connections.contracts import ProviderError
from pyracast.connections.operations import QuotaExceededError, RequestDeferredError


def exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    if isinstance(exc, ObjectDoesNotExist):
        return Response({"code": "not_found"}, status=404)
    if isinstance(exc, DjangoValidationError):
        return Response({"code": "invalid_request", "detail": exc.messages}, status=400)
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
