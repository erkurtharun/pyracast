from typing import Any

from django.core.exceptions import ObjectDoesNotExist
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from pyracast.connections.contracts import ProviderFailure
from pyracast.connections.operations import QuotaExhausted


def exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    if isinstance(exc, ObjectDoesNotExist):
        return Response({"code": "not_found"}, status=404)
    if isinstance(exc, DjangoValidationError):
        return Response({"code": "invalid_request", "detail": exc.messages}, status=400)
    if isinstance(exc, QuotaExhausted):
        return Response({"code": "quota_exhausted"}, status=429)
    if isinstance(exc, ProviderFailure):
        return Response({"code": "provider_unavailable"}, status=502)
    response = drf_exception_handler(exc, context)
    if response is not None:
        response.data = {"code": f"http_{response.status_code}", "detail": response.data}
    return response
