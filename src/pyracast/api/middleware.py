from collections.abc import Callable

from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest, HttpResponseNotAllowed
from django.utils.cache import patch_cache_control

from pyracast.accounts.audit import SecurityEvent, record_security_event
from pyracast.api.errors import refuse_in_place, with_error_contract
from pyracast.errors import ErrorCode


class PrivateAPIResponses:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if not request.path.startswith("/api/"):
            return response
        if isinstance(response, HttpResponseNotAllowed):
            # Django method guards and allauth views refuse without a body.
            response = refuse_in_place(request, response, ErrorCode.METHOD_NOT_ALLOWED)
        elif isinstance(response, HttpResponseBadRequest) and not response.content:
            # allauth refuses a malformed or non-object JSON body without one.
            response = refuse_in_place(request, response, ErrorCode.BAD_REQUEST)
        response = with_error_contract(request, response)
        patch_cache_control(response, private=True, no_store=True)
        if request.path.startswith("/api/auth/") and response.status_code == 429:
            record_security_event(SecurityEvent.AUTHENTICATION_REQUEST_THROTTLED, request=request)
        return response
