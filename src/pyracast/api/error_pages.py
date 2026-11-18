import sys

from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import Resolver404, ResolverMatch, resolve
from django.utils.cache import patch_cache_control
from django.views import csrf, defaults

from pyracast.api.errors import api_error, coded, error_json, record_refused
from pyracast.errors import ErrorCode

# Routes a browser reaches by a top-level form navigation, where a JSON body would strand the user.
PROVIDER_NAVIGATIONS = frozenset({"provider_redirect", "google_callback", "microsoft_callback"})


def _api(request: HttpRequest) -> bool:
    return request.path.startswith("/api/")


def _match(request: HttpRequest) -> ResolverMatch | None:
    # Middleware refusals arrive before URL resolution.
    if request.resolver_match is not None:
        return request.resolver_match
    try:
        return resolve(request.path_info, getattr(request, "urlconf", None))
    except Resolver404:
        return None


def _route(match: ResolverMatch | None) -> str:
    # The matched pattern, never the path, so identifiers neither reach the line nor split the
    # refusal throttle.
    return (match.route if match else None) or "-"


def _private(response: HttpResponse) -> HttpResponse:
    patch_cache_control(response, private=True, no_store=True)
    return response


def bad_request(request: HttpRequest, exception: Exception) -> HttpResponse:
    if not _api(request):
        return _private(defaults.bad_request(request, exception))
    return error_json(request, lambda: api_error(exception, request, "-"))


def permission_denied(request: HttpRequest, exception: Exception) -> HttpResponse:
    record_refused(request, ErrorCode.PERMISSION_DENIED, _route(_match(request)), None)
    if not _api(request):
        return _private(defaults.permission_denied(request, exception))
    return error_json(request, lambda: api_error(exception, request, "-"))


def page_not_found(request: HttpRequest, exception: Exception) -> HttpResponse:
    if not _api(request):
        return defaults.page_not_found(request, exception)
    return error_json(request, lambda: api_error(Http404(), request, "-"))


def server_error(request: HttpRequest) -> HttpResponse:
    if not _api(request):
        return _private(defaults.server_error(request))
    # Django calls handler500 inside its except block, so the unhandled exception is current.
    failure = sys.exc_info()[1]
    return error_json(request, lambda: api_error(failure, request, "-"))


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    match = _match(request)
    record_refused(request, ErrorCode.CSRF_FAILED, _route(match), None)
    if not _api(request):
        return _private(csrf.csrf_failure(request, reason))
    name = match.url_name if match else None
    if name in PROVIDER_NAVIGATIONS and request.headers.get("Sec-Fetch-Mode") == "navigate":
        return _private(
            HttpResponseRedirect(f"{settings.PYRACAST_SOCIAL_CALLBACK_URL}&error=expired_form")
        )
    return error_json(request, lambda: coded(ErrorCode.CSRF_FAILED, 403))
