import sys

from django.contrib.sessions.exceptions import SessionInterrupted
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.cache import patch_cache_control
from django.views import defaults

from pyracast.accounts.middleware import InvalidProxyHeaders
from pyracast.api.errors import log_server_error, server_error_body


def _api(request: HttpRequest) -> bool:
    return request.path.startswith("/api/")


def _private(response: HttpResponse) -> HttpResponse:
    patch_cache_control(response, private=True, no_store=True)
    return response


def _json(status: int, body: dict[str, str]) -> HttpResponse:
    return _private(JsonResponse(body, status=status))


def bad_request(request: HttpRequest, exception: Exception) -> HttpResponse:
    if not _api(request):
        return _private(defaults.bad_request(request, exception))
    code = "session_ended" if isinstance(exception, SessionInterrupted) else "bad_request"
    return _json(400, {"code": code})


def permission_denied(request: HttpRequest, exception: Exception) -> HttpResponse:
    if not _api(request):
        return _private(defaults.permission_denied(request, exception))
    code = (
        "invalid_proxy_headers"
        if isinstance(exception, InvalidProxyHeaders)
        else "permission_denied"
    )
    return _json(403, {"code": code})


def page_not_found(request: HttpRequest, exception: Exception) -> HttpResponse:
    if not _api(request):
        return _private(defaults.page_not_found(request, exception))
    return _json(404, {"code": "not_found"})


def server_error(request: HttpRequest) -> HttpResponse:
    if not _api(request):
        return _private(defaults.server_error(request))
    # Django calls handler500 inside its except block, so the unhandled exception is current.
    request_id = log_server_error(request, "-", sys.exc_info()[1])
    return _json(500, server_error_body(request_id))
