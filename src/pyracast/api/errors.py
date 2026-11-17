import json
import logging
import math
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import uuid4

from django.contrib.sessions.exceptions import SessionInterrupted
from django.core.exceptions import (
    BadRequest,
    ObjectDoesNotExist,
    PermissionDenied,
    SuspiciousOperation,
)
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import DatabaseError
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone, translation
from django.utils.cache import patch_cache_control
from django.utils.translation import gettext
from psycopg.errors import DeadlockDetected, LockNotAvailable, QueryCanceled
from rest_framework import exceptions
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.views import set_rollback

from pyracast.accounts.language import request_language
from pyracast.connections.contracts import ProviderError
from pyracast.connections.operations import QuotaExceededError, RequestDeferredError
from pyracast.errors import MESSAGES, DomainError, ErrorCode, ForbiddenError

logger = logging.getLogger("pyracast.api")


@dataclass(frozen=True)
class ApiError:
    status: int
    code: ErrorCode
    message: str
    fields: Mapping[str, str] | None = None
    retry_after: int | None = None
    request_id: str | None = None
    authenticate: str | None = None

    def body(self) -> dict[str, object]:
        body: dict[str, object] = {"code": self.code.value, "message": self.message}
        if self.fields:
            body["fields"] = dict(self.fields)
        if self.retry_after is not None:
            body["retry_after"] = self.retry_after
        if self.request_id is not None:
            body["request_id"] = self.request_id
        return body

    def headers(self) -> dict[str, str]:
        headers = {}
        if self.retry_after is not None:
            headers["Retry-After"] = str(self.retry_after)
        if self.authenticate is not None:
            headers["WWW-Authenticate"] = self.authenticate
        return headers


def coded(code: ErrorCode, status: int, **details: Any) -> ApiError:
    return ApiError(status, code, str(MESSAGES[code]), **details)


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


def _joined(messages: list[str]) -> str:
    return " ".join(message for message in messages if message)


def _detail_items(detail: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[str, str]]:
    if isinstance(detail, str):
        yield ".".join(path), detail
    elif isinstance(detail, dict):
        for key, value in detail.items():
            yield from _detail_items(value, (*path, str(key)))
    elif any(isinstance(item, dict | list) for item in detail):
        for index, item in enumerate(detail):
            yield from _detail_items(item, (*path, str(index)))
    else:
        yield ".".join(path), _joined(detail)


def _invalid(items: Iterator[tuple[str, str]]) -> ApiError:
    general: list[str] = []
    fields: dict[str, str] = {}
    for path, text in items:
        place = path.removesuffix(api_settings.NON_FIELD_ERRORS_KEY).removesuffix(".")
        if place:
            fields[place] = text
        else:
            general.append(text)
    message = _joined(general) or str(MESSAGES[ErrorCode.INVALID_REQUEST])
    return ApiError(400, ErrorCode.INVALID_REQUEST, message, fields or None)


def _translated(messages: list[str]) -> str:
    # Raise sites outside the API pass catalogued English literals; translate them here.
    return _joined([gettext(message) for message in messages if message])


def _django_invalid(error: DjangoValidationError) -> ApiError:
    if hasattr(error, "error_dict"):
        return _invalid(
            (key if key != "__all__" else "", _translated(messages))
            for key, messages in error.message_dict.items()
        )
    return _invalid(iter([("", _translated(error.messages))]))


def _domain(error: DomainError) -> ApiError:
    message = _joined(error.messages)
    return ApiError(
        error.status,
        error.code,
        message,
        {error.field: message} if error.field else None,
        error.retry_after,
    )


def _seconds_until(moment: datetime) -> int:
    return max(0, math.ceil((moment - timezone.now()).total_seconds()))


def _provider(error: QuotaExceededError | ProviderError) -> ApiError:
    if isinstance(error, RequestDeferredError):
        if error.available_at is None:
            return coded(ErrorCode.PROVIDER_AUTHENTICATION_FAILED, 503)
        return coded(
            ErrorCode.PROVIDER_THROTTLED, 429, retry_after=_seconds_until(error.available_at)
        )
    if isinstance(error, QuotaExceededError):
        return coded(ErrorCode.QUOTA_EXHAUSTED, 429)
    return coded(ErrorCode.PROVIDER_UNAVAILABLE, 502)


def _database(error: DatabaseError, request: HttpRequest | None, view: str) -> ApiError | None:
    if isinstance(error.__cause__, LockNotAvailable):
        return coded(ErrorCode.BUSY, 503, retry_after=1)
    if isinstance(error.__cause__, DeadlockDetected):
        return coded(ErrorCode.DATABASE_CONFLICT, 503, retry_after=1)
    if isinstance(error.__cause__, QueryCanceled):
        request_id = log_server_error(request, view, error, code="request_timeout")
        return coded(ErrorCode.REQUEST_TIMEOUT, 503, request_id=request_id)
    return None


_STATUS_CODES: Mapping[int, ErrorCode] = {
    400: ErrorCode.INVALID_REQUEST,
    401: ErrorCode.NOT_AUTHENTICATED,
    403: ErrorCode.PERMISSION_DENIED,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    406: ErrorCode.NOT_ACCEPTABLE,
    409: ErrorCode.CONFLICT,
    410: ErrorCode.SESSION_ENDED,
    415: ErrorCode.UNSUPPORTED_MEDIA_TYPE,
    429: ErrorCode.THROTTLED,
}


def status_code(status: int) -> ErrorCode:
    if status >= 500:
        return ErrorCode.SERVER_ERROR
    return _STATUS_CODES.get(status, ErrorCode.BAD_REQUEST)


def _framework(error: exceptions.APIException) -> ApiError | None:
    if isinstance(error, exceptions.ValidationError):
        return _invalid(_detail_items(error.detail))
    if isinstance(error, exceptions.NotAuthenticated | exceptions.AuthenticationFailed):
        return coded(
            ErrorCode.NOT_AUTHENTICATED,
            error.status_code,
            authenticate=getattr(error, "auth_header", None),
        )
    if isinstance(error, exceptions.PermissionDenied):
        return coded(ErrorCode(cast(str, error.get_codes())), 403)
    if isinstance(error, exceptions.Throttled):
        wait: float | None = getattr(error, "wait", None)
        return coded(
            ErrorCode.THROTTLED, 429, retry_after=None if wait is None else math.ceil(wait)
        )
    if isinstance(error, exceptions.ParseError):
        return coded(ErrorCode.BAD_REQUEST, 400)
    if error.status_code < 500:
        return coded(status_code(error.status_code), error.status_code)
    return None


def _known(error: BaseException | None, request: HttpRequest | None, view: str) -> ApiError | None:
    if isinstance(error, DomainError):
        return _domain(error)
    if isinstance(error, DjangoValidationError):
        return _django_invalid(error)
    if isinstance(error, ForbiddenError):
        return coded(error.code, 403)
    if isinstance(error, PermissionDenied):
        return coded(ErrorCode.PERMISSION_DENIED, 403)
    if isinstance(error, ObjectDoesNotExist):
        logger.warning(
            "api_object_not_found model=%s", type(error).__qualname__.removesuffix(".DoesNotExist")
        )
        return coded(ErrorCode.NOT_FOUND, 404)
    if isinstance(error, Http404):
        return coded(ErrorCode.NOT_FOUND, 404)
    if isinstance(error, SessionInterrupted):
        return coded(ErrorCode.SESSION_ENDED, 400)
    if isinstance(error, SuspiciousOperation | BadRequest):
        return coded(ErrorCode.BAD_REQUEST, 400)
    if isinstance(error, exceptions.APIException):
        return _framework(error)
    if isinstance(error, QuotaExceededError | ProviderError):
        return _provider(error)
    if isinstance(error, DatabaseError):
        return _database(error, request, view)
    return None


def api_error(error: BaseException | None, request: HttpRequest | None, view: str) -> ApiError:
    known = _known(error, request, view)
    if known is not None:
        return known
    return coded(ErrorCode.SERVER_ERROR, 500, request_id=log_server_error(request, view, error))


def _view_name(context: dict[str, Any]) -> str:
    view = context.get("view")
    return "-" if view is None else type(view).__name__


def exception_handler(exc: Exception, context: dict[str, Any]) -> Response | None:
    if isinstance(exc, SuspiciousOperation | BadRequest):
        # Django answers these through handler400 and logs them on django.security.
        return None
    set_rollback()
    error = api_error(exc, context.get("request"), _view_name(context))
    return Response(error.body(), status=error.status, headers=error.headers())


def _language(request: HttpRequest) -> str:
    return getattr(request, "LANGUAGE_CODE", None) or request_language(request, None)


def error_json(request: HttpRequest, build: Callable[[], ApiError]) -> HttpResponse:
    with translation.override(_language(request)):
        error = build()
    response = JsonResponse(error.body(), status=error.status, headers=error.headers())
    patch_cache_control(response, private=True, no_store=True)
    return response


def _status_error(
    request: HttpRequest, status: int, body: Mapping[str, Any], retry_after: str | None
) -> ApiError:
    listed = body.get("errors")
    errors = [
        error
        for error in (listed if isinstance(listed, list) else [])
        if isinstance(error, dict) and isinstance(error.get("message"), str) and error["message"]
    ]
    fields: dict[str, str] = {}
    for error in errors:
        param = error.get("param")
        if isinstance(param, str):
            fields[param] = _joined([fields.get(param, ""), error["message"]])
    code = status_code(status)
    return ApiError(
        status,
        code,
        errors[0]["message"] if errors else str(MESSAGES[code]),
        fields or None,
        int(retry_after) if retry_after and retry_after.isdecimal() else None,
        log_server_error(request, "-", None) if status >= 500 else None,
    )


def with_error_contract(request: HttpRequest, response: HttpResponse) -> HttpResponse:
    """Add the contract keys to a JSON error body that lacks them, keeping its own keys."""
    content_type = response.get("Content-Type", "").partition(";")[0].strip()
    if response.status_code < 400 or response.streaming or content_type != "application/json":
        return response
    try:
        body = json.loads(response.content)
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if {"code", "message"} <= body.keys():
        return response
    with translation.override(_language(request)):
        error = _status_error(request, response.status_code, body, response.get("Retry-After"))
    response.content = json.dumps({**body, **error.body()}).encode()
    if response.has_header("Content-Length"):
        response["Content-Length"] = str(len(response.content))
    return response
